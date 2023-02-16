'''Import statements'''
from models.MikesGraphNet import MikesGraphNet
import sys

import torch
import torch.nn as nn
from models.global_aggregation import global_aggregation
from models.model_components import general_MLP
from torch.distributions import MultivariateNormal
from ase import Atoms
from models.asymmetric_radius_graph import asymmetric_radius_graph


class molecule_graph_model(nn.Module):
    def __init__(self, dataDims, seed,
                 num_atom_feats,
                 num_mol_feats,
                 output_dimension,
                 activation,
                 num_fc_layers,
                 fc_depth,
                 fc_dropout_probability,
                 fc_norm_mode,
                 graph_model,
                 graph_filters,
                 graph_convolutional_layers,
                 concat_mol_to_atom_features,
                 pooling,
                 graph_norm,
                 num_spherical,
                 num_radial,
                 graph_convolution,
                 num_attention_heads,
                 add_spherical_basis,
                 add_torsional_basis,
                 atom_embedding_size,
                 radial_function,
                 max_num_neighbors,
                 convolution_cutoff,
                 return_latent=False,
                 crystal_mode=False,
                 crystal_convolution_type=None,
                 positional_embedding = 'sph',
                 device='cuda'):
        super(molecule_graph_model, self).__init__()
        # initialize constants and layers
        self.device = device
        self.return_latent = return_latent
        self.activation = activation
        self.num_fc_layers = num_fc_layers
        self.fc_depth = fc_depth
        self.fc_dropout_probability = fc_dropout_probability
        self.fc_norm_mode = fc_norm_mode
        self.graph_model = graph_model
        self.graph_convolution = graph_convolution
        self.output_classes = output_dimension
        self.graph_convolution_layers = graph_convolutional_layers
        self.graph_filters = graph_filters
        self.graph_norm = graph_norm
        self.num_spherical = num_spherical
        self.num_radial = num_radial
        self.num_attention_heads = num_attention_heads
        self.add_spherical_basis = add_spherical_basis
        self.add_torsional_basis = add_torsional_basis
        self.n_mol_feats = num_mol_feats  # dataDims['num mol features']
        self.n_atom_feats = num_atom_feats  # dataDims['num atom features']
        self.radial_function = radial_function
        self.max_num_neighbors = max_num_neighbors
        self.graph_convolution_cutoff = convolution_cutoff
        if not concat_mol_to_atom_features:  # if we are not adding molwise feats to atoms, subtract the dimension
            self.n_atom_feats -= self.n_mol_feats
        self.pooling = pooling
        self.fc_norm_mode = fc_norm_mode
        self.embedding_dim = atom_embedding_size
        self.crystal_mode = crystal_mode
        self.crystal_convolution_type = crystal_convolution_type

        torch.manual_seed(seed)

        if self.graph_model is not None:
            if self.graph_model == 'mike':  # mike's net - others currently deprecated. For future, implement new convolutions in the mikenet class
                self.graph_net = MikesGraphNet(
                    crystal_mode=crystal_mode,
                    crystal_convolution_type=self.crystal_convolution_type,
                    graph_convolution_filters=self.graph_filters,
                    graph_convolution=self.graph_convolution,
                    out_channels=self.fc_depth,
                    hidden_channels=self.embedding_dim,
                    num_blocks=self.graph_convolution_layers,
                    num_radial=self.num_radial,
                    num_spherical=self.num_spherical,
                    max_num_neighbors=self.max_num_neighbors,
                    cutoff=self.graph_convolution_cutoff,
                    activation='gelu',
                    embedding_hidden_dimension=self.embedding_dim,
                    num_atom_features=self.n_atom_feats,
                    norm=self.graph_norm,
                    dropout=self.fc_dropout_probability,
                    spherical_embedding=self.add_spherical_basis,
                    torsional_embedding=self.add_torsional_basis,
                    radial_embedding=self.radial_function,
                    atom_embedding_dims=dataDims['atom embedding dict sizes'],
                    attention_heads=self.num_attention_heads,
                )
            else:
                print(self.graph_model + ' is not a valid graph model!!')
                sys.exit()
        else:
            self.graph_net = nn.Identity()
            self.graph_filters = 0  # no accounting for dime inputs or outputs
            self.pools = nn.Identity()

        # initialize global pooling operation
        if self.graph_model is not None:
            self.global_pool = global_aggregation(self.pooling, self.fc_depth,
                                                  geometric_embedding = positional_embedding)

        # molecule features FC layer
        if self.n_mol_feats != 0:
            self.mol_fc = nn.Linear(self.n_mol_feats, self.n_mol_feats)

        # FC model to post-process graph fingerprint
        self.gnn_mlp = general_MLP(layers=self.num_fc_layers,
                                   filters=self.fc_depth,
                                   norm=self.fc_norm_mode,
                                   dropout=self.fc_dropout_probability,
                                   input_dim=self.fc_depth,
                                   output_dim=self.fc_depth,
                                   conditioning_dim=self.n_mol_feats,
                                   seed=seed
                                   )

        self.output_fc = nn.Linear(self.fc_depth, self.output_classes, bias=False)

    def forward(self, data, return_latent=False, return_dists=False):
        extra_outputs = {}
        if self.graph_model is not None:
            x, dists_dict = self.graph_net(data.x[:, :self.n_atom_feats], data.pos, data.batch, ptr=data.ptr, ref_mol_inds=data.aux_ind, return_dists=return_dists)  # get atoms encoding
            if self.crystal_mode:  # model only outputs ref mol atoms - many fewer
                x = self.global_pool(x, data.pos, data.batch[torch.where(data.aux_ind == 0)[0]])
            else:
                x = self.global_pool(x, data.pos, data.batch)  # aggregate atoms to molecule

        mol_feats = self.mol_fc(data.x[data.ptr[:-1], -self.n_mol_feats:])  # molecule features are repeated, only need one per molecule (hence data.ptr)

        if self.graph_model is not None:
            x = self.gnn_mlp(x, conditions=mol_feats)  # mix graph fingerprint with molecule-scale features
        else:
            x = self.gnn_mlp(mol_feats)

        output = self.output_fc(x)

        if return_dists:
            if self.graph_model is not None:
                extra_outputs['dists dict'] = dists_dict
            else:
                extra_outputs['dists dict'] = None
        if return_latent:
            extra_outputs['latent'] = output.cpu().detach().numpy()

        if len(extra_outputs) > 0:
            return output, extra_outputs
        else:
            return output

class independent_gaussian_model(nn.Module):
    def __init__(self, input_dim, means, stds, normed_length_means, normed_length_stds, cov_mat=None):
        super(independent_gaussian_model, self).__init__()

        self.input_dim = input_dim
        fixed_norms = torch.Tensor(means)
        fixed_norms[:3] = torch.Tensor(normed_length_means)
        fixed_stds = torch.Tensor(stds)
        fixed_stds[:3] = torch.Tensor(normed_length_stds)

        self.register_buffer('means', torch.Tensor(means))
        self.register_buffer('stds', torch.Tensor(stds))
        self.register_buffer('fixed_norms', torch.Tensor(fixed_norms))
        self.register_buffer('fixed_stds', torch.Tensor(fixed_stds))

        if cov_mat is not None:
            pass
        else:
            cov_mat = torch.diag(torch.Tensor(fixed_stds).pow(2))

        fixed_means = means.copy()
        fixed_means[:3] = normed_length_means
        self.prior = MultivariateNormal(fixed_norms, torch.Tensor(cov_mat))  # apply standardization
        self.dummy_params = nn.Parameter(torch.ones(100))

    def forward(self, num_samples, data):
        '''
        sample comes out in non-standardized basis, but with normalized cell lengths
        so, denormalize cell length (multiply by Z^(1/3) and vol^(1/3)
        then standardize
        '''
        # conditions are unused - dummy
        # denormalize sample before standardizing
        samples = self.prior.sample((num_samples,)).to(data.x.device)
        samples[:, :3] = samples[:, :3] * (data.Z[:, None] ** (1 / 3)) * (data.mol_volume[:, None] ** (1 / 3))
        return (samples - self.means.to(samples.device)) / self.stds.to(samples.device)  # we want samples in standardized basis

    def backward(self, samples):
        return samples * self.stds + self.means

    def score(self, samples):
        return self.prior.log_prob(samples)
