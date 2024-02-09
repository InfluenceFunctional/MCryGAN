import sys

import torch
from torch import nn, nn as nn
from torch.nn import functional as F
import numpy as np
from torch_geometric import nn as gnn
from torch_scatter import scatter, scatter_softmax

from models.asymmetric_radius_graph import asymmetric_radius_graph
from models.global_attention_aggregation import AttentionalAggregation_w_alpha
from models.vector_LayerNorm import VectorLayerNorm
from models.utils import direction_coefficient


class MLP(nn.Module):  # todo simplify and smooth out +1's and other custom methods for a general depth controller
    def __init__(self, layers, filters, input_dim, output_dim,
                 activation='gelu', seed=0, dropout=0, conditioning_dim=0,
                 norm=None, bias=True, norm_after_linear=False,
                 conditioning_mode='concat_to_first',
                 equivariant=False,
                 residue_v_to_s=False,
                 vector_output_dim=None,
                 vector_norm=None,
                 ramp_depth=False):
        super(MLP, self).__init__()
        # initialize constants and layers
        self.n_layers = layers
        self.conditioning_mode = conditioning_mode  # todo write a proper all_layer conditioning mode
        self.conditioning_dim = conditioning_dim
        self.output_dim = output_dim
        self.v_output_dim = vector_output_dim if vector_output_dim is not None else output_dim
        self.input_dim = input_dim + conditioning_dim
        self.norm_mode = norm
        self.dropout_p = dropout
        self.activation = activation
        self.bias = bias
        self.norm_after_linear = norm_after_linear
        self.equivariant = equivariant
        self.residue_v_to_s = residue_v_to_s
        self.vector_norm = vector_norm
        self.ramp_depth = ramp_depth
        if self.vector_norm:
            assert self.equivariant
        if residue_v_to_s:
            assert self.equivariant

        torch.manual_seed(seed)

        self.init_filters(filters, layers)
        self.init_scalar_transforms()
        if equivariant:
            self.init_vector_transforms()

    def init_filters(self, filters, layers):
        if isinstance(filters, list):
            self.n_filters = filters
            residue_filters = [self.input_dim] + self.n_filters

        elif self.ramp_depth:  # smoothly ramp feature depth across layers
            # linear scaling
            # self.n_filters = torch.linspace(self.input_dim, self.output_dim, self.n_layers).long().tolist()
            # log scaling for consistent growth ratio
            p = np.log(self.output_dim) / np.log(self.input_dim)
            self.n_filters = [int(self.input_dim ** (1 + (p - 1) * (i / (self.n_layers)))) for i in range(self.n_layers)]
            residue_filters = [self.input_dim] + self.n_filters
            self.same_depth = False
        else:
            self.n_filters = [filters for _ in range(layers)]

        if self.n_filters.count(self.n_filters[0]) != len(self.n_filters):  # if they are not all the same, we need residue adjustments
            self.same_depth = False
            self.residue_adjust = torch.nn.ModuleList([
                nn.Linear(residue_filters[i], residue_filters[i + 1], bias=False)
                for i in range(self.n_layers)
            ])
            if self.equivariant:
                residue_filters[0] -= self.conditioning_dim
                self.v_residue_adjust = torch.nn.ModuleList([
                    nn.Linear(residue_filters[i], residue_filters[i + 1], bias=False)
                    for i in range(self.n_layers)
                ])
        else:
            self.same_depth = True

    def init_scalar_transforms(self):
        """scalar MLP layers"""

        '''input layer'''
        if self.input_dim != self.n_filters[0]:
            self.init_layer = nn.Linear(self.input_dim, self.n_filters[0])  # set appropriate sizing
        else:
            self.init_layer = nn.Identity()

        '''working layers'''
        self.fc_layers = torch.nn.ModuleList([
            nn.Linear(self.n_filters[i] + (self.n_filters[i] if self.equivariant else 0),
                      self.n_filters[i], bias=self.bias)
            for i in range(self.n_layers)
        ])
        self.fc_activations = torch.nn.ModuleList([
            Activation(self.activation, self.n_filters[i])
            for i in range(self.n_layers)
        ])
        if self.norm_after_linear:
            self.fc_norms = torch.nn.ModuleList([
                Normalization(self.norm_mode, self.n_filters[i])
                for i in range(self.n_layers)
            ])
        else:
            self.fc_norms = torch.nn.ModuleList([
                Normalization(self.norm_mode,
                              self.n_filters[i] + (self.n_filters[i] if self.equivariant else 0))
                for i in range(self.n_layers)
            ])
        self.fc_dropouts = torch.nn.ModuleList([
            nn.Dropout(p=self.dropout_p)
            for _ in range(self.n_layers)
        ])

        '''output layer'''
        if self.output_dim != self.n_filters[-1]:
            self.output_layer = nn.Linear(self.n_filters[-1], self.output_dim, bias=False)
        else:
            self.output_layer = nn.Identity()

    def init_vector_transforms(self):
        """vector MLP layers"""
        '''input layer'''
        if self.input_dim != self.n_filters[0]:
            self.v_init_layer = nn.Linear(self.input_dim - self.conditioning_dim, self.n_filters[0], bias=False)
        else:
            self.v_init_layer = nn.Identity()

        '''working layers'''
        self.v_fc_layers = torch.nn.ModuleList([
            nn.Linear(self.n_filters[i], self.n_filters[i], bias=False)
            for i in range(self.n_layers)
        ])
        self.s_to_v_gating_layers = torch.nn.ModuleList([
            nn.Linear(self.n_filters[i], self.n_filters[i], bias=True)
            for i in range(self.n_layers)
        ])
        self.s_to_v_activations = torch.nn.ModuleList([
            Activation(self.activation, self.n_filters[i])
            for i in range(self.n_layers)
        ])
        self.v_fc_norms = torch.nn.ModuleList([
            Normalization(self.vector_norm, self.n_filters[i])
            for i in range(self.n_layers)
        ])

        '''output layer'''
        if self.v_output_dim != self.n_filters[-1]:
            self.v_output_layer = nn.Linear(self.n_filters[-1], self.v_output_dim, bias=False)
        else:
            self.v_output_layer = nn.Identity()

    def forward(self, x, v=None, conditions=None, return_latent=False, batch=None):
        if conditions is not None:
            x = torch.cat((x, conditions), dim=-1)

        x = self.init_layer(x)  # get the right feature depth
        if v is not None:
            v = self.v_init_layer(v)

        for i, (norm, linear, activation, dropout) in enumerate(zip(self.fc_norms, self.fc_layers, self.fc_activations, self.fc_dropouts)):
            x, v = self.get_residues(i, x, v)

            x = self.scalar_forward(activation, batch, dropout, linear, norm, x, v)

            if self.equivariant:
                v = self.vector_forward(i, x, v, batch)

        if not self.equivariant:
            if return_latent:
                return self.output_layer(x), x
            else:
                return self.output_layer(x)
        else:
            if return_latent:
                return self.output_layer(x), self.v_output_layer(v), x
            else:
                return self.output_layer(x), self.v_output_layer(v)

    def get_residues(self, i, x, v):
        if self.same_depth:
            x = x.clone()
        else:
            x = self.residue_adjust[i](x)
        if self.equivariant:
            if self.same_depth:
                v = v.clone()
            else:
                v = self.v_residue_adjust[i](v)
        else:
            v = None

        return x, v

    def scalar_forward(self, activation, batch, dropout, linear, norm, x, v):
        res = x.clone()
        if v is not None:  # concatenate vector lengths to scalar values
            x = torch.cat([x, torch.linalg.norm(v, dim=1)], dim=-1)

        if self.norm_after_linear:
            x = res + dropout(activation(norm(linear(x), batch=batch)))
        else:
            x = res + dropout(activation(linear(norm(x, batch=batch))))

        return x

    def vector_forward(self, i, x, v, batch):
        v = (v +
             self.s_to_v_activations[i](
                 self.s_to_v_gating_layers[i](
                     x
                 )[:, None, :]) *
             (self.v_fc_layers[i](
                 self.v_fc_norms[i](
                     v, batch=batch)
             )))  # A(FC(x)) * FC(N(v))
        return v


'''
equivariance test
>> linear scaling layer
from scipy.spatial.transform import Rotation as R

rmat = torch.tensor(R.random().as_matrix(),device=x.device, dtype=torch.float32)

v1 = v.clone()
rotv1 = torch.einsum('ij, njk->nik', rmat, v1)

y1 = v1 + F.tanh(self.s_to_v_gating_layers[i](x)[:, None, :]) * self.v_fc_layers[i](v1)
y2 = rotv1 + F.tanh(self.s_to_v_gating_layers[i](x)[:, None, :]) * self.v_fc_layers[i](rotv1)

roty1 = torch.einsum('ij, njk->nik', rmat, y1)

print(torch.mean(torch.abs(y2 - roty1)))

'''


class Normalization(nn.Module):
    def __init__(self, norm, filters, *args, **kwargs):
        super().__init__()
        self.norm_type = norm
        if norm == 'batch':
            self.norm = nn.BatchNorm1d(filters)
        elif norm == 'graph layer':
            self.norm = gnn.LayerNorm(filters)
        elif norm == 'layer':
            self.norm = nn.LayerNorm(filters)
        elif norm == 'instance':
            self.norm = nn.InstanceNorm1d(filters)  # not tested
        elif norm == 'graph':
            self.norm = gnn.GraphNorm(filters)
        elif norm == 'graph vector layer':
            self.norm = VectorLayerNorm(filters, mode='graph')
        elif norm == 'vector layer':
            self.norm = VectorLayerNorm(filters, mode='node')
        elif norm is None:
            self.norm = nn.Identity()
        else:
            print(norm + " is not a valid normalization")
            sys.exit()

    def forward(self, x, batch=None):
        if batch is not None and self.norm_type != 'batch' and self.norm_type != 'layer' and self.norm_type is not None:
            return self.norm(x, batch)

        return self.norm(x)


class Activation(nn.Module):
    def __init__(self, activation_func, filters, *args, **kwargs):
        super().__init__()
        if activation_func.lower() == 'relu':
            self.activation = F.relu
        elif activation_func.lower() == 'gelu':
            self.activation = F.gelu
        elif activation_func.lower() == 'kernel':  # rather expensive
            self.activation = kernelActivation(n_basis=10, span=4, channels=filters)
        elif activation_func.lower() == 'leaky relu':
            self.activation = F.leaky_relu

    def forward(self, input):
        return self.activation(input)


class kernelActivation(nn.Module):  # a better (pytorch-friendly) implementation of activation as a linear combination of basis functions
    def __init__(self, n_basis, span, channels, *args, **kwargs):
        super(kernelActivation, self).__init__(*args, **kwargs)

        self.channels, self.n_basis = channels, n_basis
        # define the space of basis functions
        self.register_buffer('dict', torch.linspace(-span, span, n_basis))  # positive and negative values for Dirichlet Kernel
        gamma = 1 / (6 * (self.dict[-1] - self.dict[-2]) ** 2)  # optimum gaussian spacing parameter should be equal to 1/(6*spacing^2) according to KAFnet paper
        self.register_buffer('gamma', torch.ones(1) * gamma)  #

        # self.register_buffer('dict', torch.linspace(0, n_basis-1, n_basis)) # positive values for ReLU kernel

        # define module to learn parameters
        # 1d convolutions allow for grouping of terms, unlike nn.linear which is always fully-connected.
        # #This way should be fast and efficient, and play nice with pytorch optim
        self.linear = nn.Conv1d(channels * n_basis, channels, kernel_size=(1, 1), groups=int(channels), bias=False)

        # nn.init.normal(self.linear.weight.data, std=0.1)

    def kernel(self, x):
        # x has dimention batch, features, y, x
        # must return object of dimension batch, features, y, x, basis
        x = x.unsqueeze(2)
        if len(x) == 2:
            x = x.reshape(2, self.channels, 1)

        return torch.exp(-self.gamma * (x - self.dict) ** 2)

    def forward(self, x):
        x = self.kernel(x).unsqueeze(-1).unsqueeze(-1)  # run activation, output shape batch, features, y, x, basis
        x = x.reshape(x.shape[0], x.shape[1] * x.shape[2], x.shape[3], x.shape[4])  # concatenate basis functions with filters
        x = self.linear(x).squeeze(-1).squeeze(-1)  # apply linear coefficients and sum

        return x


def construct_radial_graph(pos, batch, ptr, cutoff, max_num_neighbors, aux_ind=None):
    """
    construct edge indices over a radial graph
    optionally, compute intra (within ref_mol_inds) and inter (between ref_mol_inds and outside inds) edges
    """
    if aux_ind is not None:
        inside_inds = torch.where(aux_ind == 0)[0]
        outside_inds = torch.where(aux_ind == 1)[0]  # atoms which are not in the asymmetric unit but which we will convolve - pre-excluding many from outside the cutoff
        inside_batch = batch[inside_inds]  # get the feature vectors we want to repeat
        n_repeats = [int(torch.sum(batch == ii) / torch.sum(inside_batch == ii)) for ii in range(len(ptr) - 1)]  # number of molecules in convolution region

        # intramolecular edges
        edge_index = asymmetric_radius_graph(pos, batch=batch, r=cutoff,  # intramolecular interactions - stack over range 3 convolutions
                                             max_num_neighbors=max_num_neighbors, flow='source_to_target',
                                             inside_inds=inside_inds, convolve_inds=inside_inds)

        # intermolecular edges
        edge_index_inter = asymmetric_radius_graph(pos, batch=batch, r=cutoff,  # extra radius for intermolecular graph convolution
                                                   max_num_neighbors=max_num_neighbors, flow='source_to_target',
                                                   inside_inds=inside_inds, convolve_inds=outside_inds)

        return {'edge_index': edge_index, 'edge_index_inter': edge_index_inter, 'inside_inds': inside_inds,
                'outside_inds': outside_inds, 'inside_batch': inside_batch, 'n_repeats': n_repeats}

    else:

        edge_index = gnn.radius_graph(pos, r=cutoff, batch=batch,
                                      max_num_neighbors=max_num_neighbors, flow='source_to_target')  # note - requires batch be monotonically increasing

        return {'edge_index': edge_index}


class GlobalAggregation(nn.Module):
    """
    wrapper for several types of global aggregation functions
    NOTE - I believe PyG might have a new built-in method which does exactly this
    """

    def __init__(self, agg_func, depth):  # todo rewrite this with new pyg aggr class and/or custom functions (e.g., scatter)
        super(GlobalAggregation, self).__init__()
        self.agg_func = agg_func
        if agg_func == 'mean':
            self.agg = gnn.global_mean_pool
        elif agg_func == 'sum':
            self.agg = gnn.global_add_pool
        elif agg_func == 'max':
            self.agg = gnn.global_max_pool
        elif self.agg_func == '1o max':
            pass
        elif agg_func == 'attention':
            self.agg = gnn.GlobalAttention(nn.Sequential(nn.Linear(depth, depth), nn.LeakyReLU(), nn.Linear(depth, 1)))
        elif agg_func == 'set2set':
            self.agg = gnn.Set2Set(in_channels=depth, processing_steps=4)
            self.agg_fc = nn.Linear(depth * 2, depth)  # condense to correct number of filters
        elif agg_func == 'simple combo':
            self.agg_list1 = [gnn.global_max_pool, gnn.global_mean_pool, gnn.global_add_pool]  # simple aggregation functions
            self.agg_fc = MLP(
                layers=1,
                filters=depth,
                input_dim=depth * (len(self.agg_list1)),
                output_dim=depth,
                norm=None,
                dropout=0)  # condense to correct number of filters
        elif agg_func == 'mean sum':  # todo add a max aggregator which picks max by vector length (equivariant!)
            pass
        elif agg_func == 'combo':
            self.agg_list1 = [gnn.global_max_pool, gnn.global_mean_pool, gnn.global_add_pool]  # simple aggregation functions
            self.agg_list2 = nn.ModuleList([gnn.GlobalAttention(
                MLP(input_dim=depth,
                    output_dim=1,
                    layers=1,
                    filters=depth,
                    activation='leaky relu',
                    norm=None),
            )])  # aggregation functions requiring parameters
            self.agg_fc = MLP(
                layers=1,
                filters=depth,
                input_dim=depth * (len(self.agg_list1) + 1),
                output_dim=depth,
                norm=None,
                dropout=0)  # condense to correct number of filters
        elif agg_func == 'molwise':
            self.agg = gnn.pool.max_pool_x
        elif agg_func == 'equivariant attention':
            self.agg = AttentionalAggregation_w_alpha(
                MLP(input_dim=depth,
                    output_dim=1,
                    layers=1,
                    filters=depth,
                    activation='leaky relu',
                    norm=None)
            )
        elif agg_func == 'equivariant combo':
            self.agg = AttentionalAggregation_w_alpha(
                MLP(input_dim=depth,
                    output_dim=1,
                    layers=1,
                    filters=depth,
                    activation='leaky relu',
                    norm=None)
            )
            self.agg_norm = Normalization('graph vector layer', depth * 3)
            self.agg_fc = nn.Linear(depth * 3, depth, bias=False)
        elif agg_func is None:
            self.agg = nn.Identity()

        if agg_func == 'equivariant max':
            print("WARNING Equivariant max pooling is mostly but not 100% equivariant, e.g., in degenerate cases")

    def forward(self, x, batch, cluster=None, output_dim=None, v=None):
        if self.agg_func == 'set2set':
            x = self.agg(x, batch, size=output_dim)
            return self.agg_fc(x)
        elif self.agg_func == 'combo':
            output1 = [agg(x, batch, size=output_dim) for agg in self.agg_list1]
            output2 = [agg(x, batch, size=output_dim) for agg in self.agg_list2]
            # output3 = [agg(x, batch, 3, size = output_dim) for agg in self.agg_list3]
            return self.agg_fc(torch.cat((output1 + output2), dim=1))
        elif self.agg_func == 'simple combo':
            output1 = [agg(x, batch, size=output_dim) for agg in self.agg_list1]
            return self.agg_fc(torch.cat(output1, dim=1))
        elif self.agg_func is None:
            return x  # do nothing
        elif self.agg_func == 'molwise':
            return self.agg(cluster=cluster, batch=batch, x=x)[0]
        elif self.agg_func == 'mean sum':
            return (scatter(x, batch, dim_size=output_dim, dim=0, reduce='mean') +
                    scatter(x, batch, dim_size=output_dim, dim=0, reduce='sum'))
        elif self.agg_func == 'equivariant max':
            # assume the input is nx3xk dimensional. Imperfectly equivariant
            agg = torch.stack([v[batch == bind][x[batch == bind].argmax(dim=0), :, torch.arange(v.shape[-1])] for bind in range(batch[-1] + 1)])
            return scatter(x, batch, dim_size=output_dim, dim=0, reduce='max'), agg
        elif self.agg_func == 'equivariant softmax':
            weights = scatter_softmax(torch.linalg.norm(v, dim=1), batch, dim=0)
            return (scatter(weights * x, batch, dim_size=output_dim, dim=0, reduce='sum'),
                    scatter(weights[:, None, :] * v, batch, dim=0, dim_size=output_dim, reduce='sum'))
        elif self.agg_func == 'equivariant combo':
            scalar_agg, alpha = self.agg(x, batch, dim_size=output_dim, return_alpha=True)
            agg1 = scatter(alpha[:, 0, None, None] * v, batch, dim=0, dim_size=output_dim, reduce='sum')  # use the same attention weights for vector aggregation
            agg2 = scatter(v, batch, dim_size=output_dim, dim=0, reduce='mean')
            agg3 = scatter(v, batch, dim_size=output_dim, dim=0, reduce='sum')

            return scalar_agg, self.agg_fc(
                self.agg_norm(
                    torch.cat([agg1, agg2, agg3], dim=-1),
                    batch=torch.arange(len(agg1), device=agg1.device, dtype=torch.long)))  # return num_graphsx3xk
        elif self.agg_func == 'equivariant attention':
            scalar_agg, alpha = self.agg(x, batch, dim_size=output_dim, return_alpha=True)
            vector_agg = scatter(alpha[:, 0, None, None] * v, batch, dim=0, dim_size=output_dim, reduce='sum')  # use the same attention weights for vector aggregation
            return scalar_agg, vector_agg
        else:
            return self.agg(x, batch, size=output_dim)


'''
from scipy.spatial.transform import Rotation as R

rmat = torch.tensor(R.random().as_matrix(),device=x.device, dtype=torch.float32)

v1 = v.clone()
rotv1 = torch.einsum('ij, njk->nik', rmat, v1)

weights = scatter_softmax(torch.linalg.norm(v, dim=1), batch, dim=0)

y1 = scatter(weights[:, None, :] * v1, batch, dim=0, dim_size=output_dim, reduce='sum')
y2 = scatter(weights[:, None, :] * rotv1, batch, dim=0, dim_size=output_dim, reduce='sum')

roty1 = torch.einsum('ij, njk->nik', rmat, y1)

print(torch.mean(torch.abs(y2 - roty1)))
'''
