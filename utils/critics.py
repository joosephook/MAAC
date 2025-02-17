import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from itertools import chain


class AttentionCritic(nn.Module):
    """
    Attention network, used as critic for all agents. Each agent gets its own
    observation and action, and can also attend over the other agents' encoded
    observations and actions.
    """
    def __init__(self, sa_sizes, hidden_dim=32, norm_in=True, attend_heads=1):
        """
        Inputs:
            sa_sizes (list of (int, int)): Size of state and action spaces per
                                          agent
            hidden_dim (int): Number of hidden dimensions
            norm_in (bool): Whether to apply BatchNorm to input
            attend_heads (int): Number of attention heads to use (use a number
                                that hidden_dim is divisible by)
        """
        super(AttentionCritic, self).__init__()
        assert (hidden_dim % attend_heads) == 0
        self.sa_sizes = sa_sizes
        self.nagents = len(sa_sizes)
        self.attend_heads = attend_heads

        self.critic_encoders = nn.ModuleList()
        self.critics = nn.ModuleList()

        self.state_encoders = nn.ModuleList()
        # iterate over agents
        for sdim, adim in sa_sizes:
            idim = sdim + adim
            odim = adim
            encoder = nn.Sequential()
            if norm_in:
                encoder.add_module('enc_bn', nn.BatchNorm1d(idim,
                                                            affine=False))
            encoder.add_module('enc_fc1', nn.Linear(idim, hidden_dim))
            encoder.add_module('enc_nl', nn.LeakyReLU())
            self.critic_encoders.append(encoder)
            critic = nn.Sequential()
            critic.add_module('critic_fc1', nn.Linear(2 * hidden_dim,
                                                      hidden_dim))
            critic.add_module('critic_nl', nn.LeakyReLU())
            critic.add_module('critic_fc2', nn.Linear(hidden_dim, odim))
            self.critics.append(critic)

            state_encoder = nn.Sequential()
            if norm_in:
                state_encoder.add_module('s_enc_bn', nn.BatchNorm1d(
                                            sdim, affine=False))
            state_encoder.add_module('s_enc_fc1', nn.Linear(sdim,
                                                            hidden_dim))
            state_encoder.add_module('s_enc_nl', nn.LeakyReLU())
            self.state_encoders.append(state_encoder)

        attend_dim = hidden_dim // attend_heads
        self.key_extractors = nn.ModuleList()
        self.selector_extractors = nn.ModuleList()
        self.value_extractors = nn.ModuleList()
        for i in range(attend_heads):
            self.key_extractors.append(nn.Linear(hidden_dim, attend_dim, bias=False))
            self.selector_extractors.append(nn.Linear(hidden_dim, attend_dim, bias=False))
            self.value_extractors.append(nn.Sequential(nn.Linear(hidden_dim,
                                                                attend_dim),
                                                       nn.LeakyReLU()))

        self.shared_modules = [self.key_extractors, self.selector_extractors,
                               self.value_extractors, self.critic_encoders]

    def shared_parameters(self):
        """
        Parameters shared across agents and reward heads
        """
        return chain(*[m.parameters() for m in self.shared_modules])

    def scale_shared_grads(self):
        """
        Scale gradients for parameters that are shared since they accumulate
        gradients from the critic loss function multiple times
        """
        for p in self.shared_parameters():
            p.grad.data.mul_(1. / self.nagents)

    def forward(self, inps, agents=None, return_q=True, return_all_q=False,
                regularize=False, return_attend=False, logger=None, niter=0):
        """
        Inputs:
            inps (list of PyTorch Matrices): Inputs to each agents' encoder
                                             (batch of obs + ac)
            agents (int): indices of agents to return Q for
            return_q (bool): return Q-value
            return_all_q (bool): return Q-value for all actions
            regularize (bool): returns values to add to loss function for
                               regularization
            return_attend (bool): return attention weights per agent
            logger (TensorboardX SummaryWriter): If passed in, important values
                                                 are logged
        """
        if agents is None:
            agents = range(len(self.critic_encoders))
        states = [s for s, a in inps]
        actions = [a for s, a in inps]
        inps = [torch.cat((s, a), dim=1) for s, a in inps]
        # extract state-action encoding for each agent
        sa_encodings = [encoder(inp) for encoder, inp in zip(self.critic_encoders, inps)]
        # extract state encoding for each agent that we're returning Q for
        s_encodings = [self.state_encoders[a_i](states[a_i]) for a_i in agents]
        # extract keys for each head for each agent
        all_head_keys = [[k_ext(enc) for enc in sa_encodings] for k_ext in self.key_extractors]
        # extract sa values for each head for each agent
        all_head_values = [[v_ext(enc) for enc in sa_encodings] for v_ext in self.value_extractors]
        # extract selectors for each head for each agent that we're returning Q for
        all_head_selectors = [[sel_ext(enc) for i, enc in enumerate(s_encodings) if i in agents]
                              for sel_ext in self.selector_extractors]

        other_all_values = [[] for _ in range(len(agents))]
        all_attend_logits = [[] for _ in range(len(agents))]
        all_attend_probs = [[] for _ in range(len(agents))]
        # calculate attention per head
        for curr_head_keys, curr_head_values, curr_head_selectors in zip(
                all_head_keys, all_head_values, all_head_selectors):
            # iterate over agents
            for i, a_i, selector in zip(range(len(agents)), agents, curr_head_selectors):
                keys = [k for j, k in enumerate(curr_head_keys) if j != a_i]
                values = [v for j, v in enumerate(curr_head_values) if j != a_i]
                # calculate attention across agents
                attend_logits = torch.matmul(selector.view(selector.shape[0], 1, -1),
                                             torch.stack(keys).permute(1, 2, 0))
                # scale dot-products by size of key (from Attention is All You Need)
                scaled_attend_logits = attend_logits / np.sqrt(keys[0].shape[1])
                attend_weights = F.softmax(scaled_attend_logits, dim=2)
                other_values = (torch.stack(values).permute(1, 2, 0) *
                                attend_weights).sum(dim=2)
                other_all_values[i].append(other_values)
                all_attend_logits[i].append(attend_logits)
                all_attend_probs[i].append(attend_weights)
        # calculate Q per agent
        all_rets = []
        for i, a_i in enumerate(agents):
            head_entropies = [(-((probs + 1e-8).log() * probs).squeeze().sum(1)
                               .mean()) for probs in all_attend_probs[i]]
            agent_rets = []
            critic_in = torch.cat((s_encodings[i], *other_all_values[i]), dim=1)
            all_q = self.critics[a_i](critic_in)
            int_acs = actions[a_i].max(dim=1, keepdim=True)[1]
            q = all_q.gather(1, int_acs)
            if return_q:
                agent_rets.append(q)
            if return_all_q:
                agent_rets.append(all_q)
            if regularize:
                # regularize magnitude of attention logits
                attend_mag_reg = 1e-3 * sum((logit**2).mean() for logit in
                                            all_attend_logits[i])
                regs = (attend_mag_reg,)
                agent_rets.append(regs)
            if return_attend:
                agent_rets.append(np.array(all_attend_probs[i]))
            if logger is not None:
                logger.add_scalars('agent%i/attention' % a_i,
                                   dict(('head%i_entropy' % h_i, ent) for h_i, ent
                                        in enumerate(head_entropies)),
                                   niter)
            if len(agent_rets) == 1:
                all_rets.append(agent_rets[0])
            else:
                all_rets.append(agent_rets)
        if len(all_rets) == 1:
            return all_rets[0]
        else:
            return all_rets


class SelectiveAttentionNetwork(nn.Module):
    def __init__(self, input_dim, output_dim, widths, hidden_layers, selector_width, selector_depth):
        super(SelectiveAttentionNetwork, self).__init__()
        assert selector_depth >= 1, "Need at least one hidden layer for selector"
        assert len(widths) == len(hidden_layers), "Mismatch between no. of widths and hidden layers for subnetworks"

        self.selector_layers = []
        self.strains = []
        self.strain_params = nn.ModuleList()
        self.strain_hidden_activation = nn.ReLU()
        # create the different strains/subnetworks
        for w, d in zip(widths, hidden_layers):
            s = nn.Linear(input_dim, w)
            strain = [s]
            self.strain_params.append(s)
            self.selector_layers.append(s.weight)

            for __ in range(d-1):
                s = nn.Linear(w, w)
                strain.append(s)
                self.strain_params.append(s)

            s = nn.Linear(w, output_dim)
            strain.append(s)
            self.strain_params.append(s)
            self.strains.append(strain)

        # create selector network architecture
        self.selector_input_dim = input_dim + output_dim * len(widths)
        self.selector_output_dim = len(widths)
        selector_width = self.selector_input_dim
        self.selector_hidden_activation = nn.ReLU()

        s = nn.Linear(self.selector_input_dim, selector_width)
        self.selector = [s]
        self.selector_params = nn.ModuleList()
        self.selector_params.append(s)
        self.selector_output_activation = nn.Softmax(dim=0)

        for __ in range(selector_depth-1):
            s = nn.Linear(selector_width, selector_width)
            self.selector.append(s)
            self.selector_params.append(s)

        s = nn.Linear(self.selector_input_dim, self.selector_output_dim)
        self.selector.append(s)
        self.selector_params.append(s)


    def forward(self, input):
        # define forward pass through network
        strain_outputs = []

        for strain in self.strains:
            x = input

            for layer in strain[:-1]:
                x = layer(x)
                x = self.strain_hidden_activation(x)
            else:
                # final layer output
                x = strain[-1](x)
                strain_outputs.append(x)

        # concatenate strain outputs horizontally to single vector
        strain_outs = torch.cat(tuple(strain_outputs), dim=-1)

        # create selector input vector
        selector_input = [strain_outs]
        selector_input.append(input)
        selector_input = torch.cat(tuple(selector_input), dim=-1)

        # score the output
        x = selector_input
        for layer in self.selector[:-1]:
            x = layer(x)
            x = self.selector_hidden_activation(x)
        else:
            x = self.selector[-1](x)
            scores = self.selector_output_activation(x)


        outs = []
        for i in range(len(strain_outputs)):
            score = scores[:, i:i+1].repeat(1, 5)
            out = strain_outputs[i]
            scored_out = out * score
            outs.append(scored_out)
        else:
            output = sum(outs)


        return output

    def get_selector_layers(self):
        return self.selector_layers

class AttentionNetwork(nn.Module):
    def __init__(self, input_dim, output_dim, widths, hidden_layers, selector_width, selector_depth):
        super(AttentionNetwork, self).__init__()
        assert selector_depth >= 1, "Need at least one hidden layer for selector"
        assert len(widths) == len(hidden_layers), "Mismatch between no. of widths and hidden layers for subnetworks"

        self.strains = []
        self.strain_params = nn.ModuleList()
        self.strain_hidden_activation = nn.ReLU()
        # create the different strains/subnetworks
        for w, d in zip(widths, hidden_layers):
            s = nn.Linear(input_dim, w)
            strain = [s]
            self.strain_params.append(s)

            for __ in range(d-1):
                s = nn.Linear(w, w)
                strain.append(s)
                self.strain_params.append(s)

            s = nn.Linear(w, output_dim)
            strain.append(s)
            self.strain_params.append(s)
            self.strains.append(strain)

    def forward(self, input):
        # define forward pass through network
        strain_outputs = []

        for strain in self.strains:
            x = input

            for layer in strain[:-1]:
                x = layer(x)
                x = self.strain_hidden_activation(x)
            else:
                # final layer output
                x = strain[-1](x)
                strain_outputs.append(x)

        # concatenate strain outputs horizontally to single vector
        strain_outs = torch.stack(strain_outputs, dim=0).sum(0)

        return strain_outs

class SelectiveAttentionCritic(nn.Module):
    """
    Attention network, used as critic for all agents. Each agent gets its own
    observation and action, and can also attend over the other agents' encoded
    observations and actions.
    """
    def __init__(self, sa_sizes, widths, hidden_layers, selector_width, selector_depth, with_selector=True, **kwargs):
        """
        Inputs:
            sa_sizes (list of (int, int)): Size of state and action spaces per
                                          agent
            hidden_dim (int): Number of hidden dimensions
            norm_in (bool): Whether to apply BatchNorm to input
            attend_heads (int): Number of attention heads to use (use a number
                                that hidden_dim is divisible by)
        """
        super(SelectiveAttentionCritic, self).__init__()
        self.sa_sizes = sa_sizes
        self.nagents = len(sa_sizes)
        self.full_input_size = np.sum(sa_sizes)
        self.with_selector = with_selector

        self.critics = nn.ModuleList()

        # iterate over agents
        for sdim, adim in sa_sizes:
            #critic = self.build_critic(sa_sizes, hidden_dim, norm_in, attend_heads)
            # initialize critic
            idim = sdim + adim
            odim = adim

            # def __init__(self, input_dim, output_dim, widths, hidden_layers, selector_width, selector_depth):
            # create critics
            if with_selector:
                CriticNetwork = SelectiveAttentionNetwork
            else:
                CriticNetwork = AttentionNetwork
            critic = CriticNetwork(input_dim=self.full_input_size,
                                               output_dim=odim,
                                               widths=widths,
                                               hidden_layers=hidden_layers,
                                               selector_width=selector_width,
                                               selector_depth=selector_depth
                                               )
            self.critics.append(critic)

    def forward(self, inps, agents=None, return_q=True, return_all_q=False,
                regularize=False, return_attend=False, logger=None, niter=0):
        """
        Inputs:
            inps (list of PyTorch Matrices): Inputs to each agents' encoder
                                             (batch of obs + ac)
            agents (int): indices of agents to return Q for
            return_q (bool): return Q-value
            return_all_q (bool): return Q-value for all actions
            regularize (bool): returns values to add to loss function for
                               regularization
            return_attend (bool): return attention weights per agent
            logger (TensorboardX SummaryWriter): If passed in, important values
                                                 are logged
        """
        if agents is None:
            agents = range(self.nagents)

        # leave this in for compatibility
        states = [s for s, a in inps]
        actions = [a for s, a in inps]
        inps = [torch.cat((s, a), dim=1) for s, a in inps]

        # calculate Q per agent
        all_rets = []
        for i, a_i in enumerate(agents):
            agent_rets = []
            critic_in = torch.cat(tuple(inps), dim=1)
            all_q = self.critics[a_i](critic_in)
            int_acs = actions[a_i].max(dim=1, keepdim=True)[1]
            q = all_q.gather(1, int_acs)
            if return_q:
                agent_rets.append(q)
            if return_all_q:
                agent_rets.append(all_q)
            if regularize:
                selectors = self.critics[a_i].get_selector_layers();
                weight_sum = [torch.sum(w) for w in selectors]
                l1_loss = sum(weight_sum)
                agent_rets.append((l1_loss,))
                # regularize magnitude of attention logits
                # attend_mag_reg = 1e-3 * sum((logit**2).mean() for logit in
                #                             all_attend_logits[i])
                # regs = (attend_mag_reg,)
                # agent_rets.append(regs)
            if return_attend:
                # agent_rets.append(np.array(all_attend_probs[i]))
                pass
            if logger is not None:
                # logger.add_scalars('agent%i/attention' % a_i,
                #                    dict(('head%i_entropy' % h_i, ent) for h_i, ent
                #                         in enumerate(head_entropies)),
                #                    niter)
                pass
            if len(agent_rets) == 1:
                all_rets.append(agent_rets[0])
            else:
                all_rets.append(agent_rets)
        if len(all_rets) == 1:
            return all_rets[0]
        else:
            return all_rets