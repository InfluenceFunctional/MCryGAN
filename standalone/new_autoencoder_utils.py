from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from models.base_models import molecule_graph_model
from models.components import MLP
from scipy.spatial.distance import cdist
from scipy.optimize import linear_sum_assignment
import math
import numbers


class GaussianSmoothing(nn.Module):
    """
    https://discuss.pytorch.org/t/is-there-anyway-to-do-gaussian-filtering-for-an-image-2d-3d-in-pytorch/12351/10
    Apply gaussian smoothing on a
    1d, 2d or 3d tensor. Filtering is performed seperately for each channel
    in the input using a depthwise convolution.
    Arguments:
        channels (int, sequence): Number of channels of the input tensors. Output will
            have this number of channels as well.
        kernel_size (int, sequence): Size of the gaussian kernel.
        sigma (float, sequence): Standard deviation of the gaussian kernel.
        dim (int, optional): The number of dimensions of the data.
            Default value is 2 (spatial).
    """

    def __init__(self, channels, kernel_size, sigma, dim=2):
        super(GaussianSmoothing, self).__init__()
        if isinstance(kernel_size, numbers.Number):
            kernel_size = [kernel_size] * dim
        if isinstance(sigma, numbers.Number):
            sigma = [sigma] * dim

        # The gaussian kernel is the product of the
        # gaussian function of each dimension.
        kernel = 1
        meshgrids = torch.meshgrid(
            [
                torch.arange(size, dtype=torch.float32)
                for size in kernel_size
            ]
        )
        for size, std, mgrid in zip(kernel_size, sigma, meshgrids):
            mean = (size - 1) / 2
            kernel *= 1 / (std * math.sqrt(2 * math.pi)) * \
                      torch.exp(-((mgrid - mean) / std) ** 2 / 2)

        # Make sure sum of values in gaussian kernel equals 1.
        kernel = kernel / torch.sum(kernel)

        # Reshape to depthwise convolutional weight
        kernel = kernel.view(1, 1, *kernel.size())
        kernel = kernel.repeat(channels, *[1] * (kernel.dim() - 1))

        self.register_buffer('weight', kernel)
        self.groups = channels

        if dim == 1:
            self.conv = F.conv1d
        elif dim == 2:
            self.conv = F.conv2d
        elif dim == 3:
            self.conv = F.conv3d
        else:
            raise RuntimeError(
                'Only 1, 2 and 3 dimensions are supported. Received {}.'.format(dim)
            )

    def forward(self, input):
        """
        Apply gaussian filter to input.
        Arguments:
            input (torch.Tensor): Input to apply gaussian filter on.
        Returns:
            filtered (torch.Tensor): Filtered output.
        """
        return self.conv(input, weight=self.weight, groups=self.groups)


def get_reconstruction_likelihood(data, decoded_data, sigma):
    """
    compute the overlap of 3D gaussians centered on points in the target data
    with those in the predicted data. Each gaussian in the target should have an overlap totalling 1.

    do this independently for each class

    scale predicted points gaussian heights by their confidence in each class

    sigma must be significantly smaller than inter-particle distances in the target data
    """

    dists = torch.cdist(data.pos, decoded_data.pos, p=2)  # n_targets x n_guesses
    overlap = torch.exp(-dists ** 2 / sigma)

    target_types = data.x[:, 0]
    target_probs = decoded_data.x[:, target_types].diag()

    # scale all overlaps by the predicted confidence in each particle type
    scaled_overlap = overlap * target_probs

    # did this for all graphs combined, now split into graphwise components
    # todo accelerate with scatter
    filtered_scaled_overlap = torch.cat([
        scaled_overlap[data.batch == ind][:, data.batch == ind].sum(1) for ind in range(data.num_graphs)
    ])

    loss = 10 * F.smooth_l1_loss(filtered_scaled_overlap, torch.ones_like(filtered_scaled_overlap))  # overlaps should all be exactly 1
    return loss


def get_reconstruction_likelihood_discrete(smoother, data, decoded_data, num_particle_types, num_bins=100, hist_range=[-1, 1]):
    """
    compare predicted types & coordinates to 3D gaussian fields
    """
    real_particle_coords = data.pos
    real_particle_probs = F.one_hot(data.x[:, 0], num_particle_types).float()

    decoded_particle_coords = decoded_data.pos
    decoded_particle_probs = decoded_data.x

    smoothed_target = get_smoothed_density(real_particle_coords, real_particle_probs, smoother, hist_range, num_particle_types, data, num_bins)
    smoothed_guess = get_smoothed_density(decoded_particle_coords, decoded_particle_probs, smoother, hist_range, num_particle_types, decoded_data, num_bins)
    hist_overlap = torch.sum(torch.min(smoothed_target, smoothed_guess), dim=(1, 2, 3, 4)) / data.mol_size

    if torch.round(smoothed_target.sum()) != len(data.x) or torch.round(smoothed_guess.sum()) != len(data.x):
        print("Warning, density grid is either insufficiently large or insufficiently dense for given configs")

    return (-torch.log(hist_overlap.clip(min=1e-6))).mean()

    # return F.binary_cross_entropy(smoothed_guess, smoothed_target)


def get_smoothed_density(coords, probs, smoother, hist_range, num_particle_types, data, num_bins):
    particle_discrete_indices = torch.bucketize(coords, torch.linspace(hist_range[0], hist_range[1], num_bins + 1, device='cuda')) - 1

    # target distribution in discretized classwise space
    target = torch.zeros((data.num_graphs, num_bins, num_bins, num_bins, num_particle_types), dtype=torch.float32, device='cuda')
    for ii in range(data.num_graphs):
        target[
        ii,
        particle_discrete_indices[data.batch == ii, 0],
        particle_discrete_indices[data.batch == ii, 1],
        particle_discrete_indices[data.batch == ii, 2], :] = probs[data.batch == ii]

    # assert torch.abs(target.sum() - probs.sum()) < 1e-3, "Multiple Particles in a single bin, increase bin density!"

    # target = F.one_hot(target, num_classes=num_particle_types + 1).float()
    target = torch.permute(target, (0, 4, 1, 2, 3))

    pad = smoother.weight.shape[-1]  # // 2
    smoothed_target = torch.stack([F.pad(smoother(target[i]), (pad, pad, pad, pad, pad, pad), mode='constant') for i in range(data.num_graphs)])

    # assert torch.abs(smoothed_target.sum() - probs.sum()) < 1e-2

    return smoothed_target

    # viz target
    # fig = go.Figure()
    # sample_true = target[0].argmax(0).cpu().detach().numpy()
    # x, y, z = sample_true.nonzero()
    # fig.add_trace(go.Scatter3d(
    #     x=x, y=y, z=z,
    #     mode='markers',
    #     showlegend=True,
    #     marker=dict(
    #         size=10,
    #         color=sample_true[x, y, z],
    #         colorscale='Jet',
    #         cmin=0,
    #         opacity=0.5
    #     )))
    # fig.update_layout(showlegend=True)
    # fig.show()


def get_reconstruction_likelihood_old(data, decoding, sigma: float = 1):
    """
    compare predicted types & coordinates to 3D gaussian fields
    """
    cov_mat = torch.eye(3, dtype=torch.float, device=data.x.device) * sigma
    cov_det = torch.prod(torch.diag(cov_mat))  # true for diagonal covariance matrix
    cov_inv = torch.eye(3, dtype=torch.float, device=data.x.device) / sigma  # true for diagonal

    norm = (2 * torch.pi * torch.ones(1, dtype=torch.float, device=data.x.device)) ** (-3 / 2)
    norm2 = 1 / (torch.sqrt(cov_det)) * norm

    types_raw = decoding[:, 3:]
    types_prod = F.softmax(types_raw, dim=-1)
    pos_pred = decoding[:, :3]

    # for testing - this is the true data, should have a higher probability
    # types_prod = F.one_hot(data.x[:, 0], num_classes=5)
    # pos_pred = data.pos

    # gaussians centered on true locations - batch of all at once
    # below is the 3D gaussian for unit covariance
    point_density = lambda x, points: norm2 * torch.exp(-0.5 * torch.sum((x - points) ** 2, dim=1) * cov_inv[0, 0])  # valid for unit covariance

    graphwise_probs = [[] for _ in range(data.num_graphs)]
    graph_inds = []
    type_inds = []
    for graph_ind in range(data.num_graphs):
        graph_inds.append(data.batch == graph_ind)

    for type_ind in range(types_raw.shape[1]):
        type_inds.append(data.x[:, 0] == type_ind)

    for graph_ind in range(data.num_graphs):  # todo could no doubt be sped up
        for type_ind in range(types_raw.shape[1]):
            relevant_inds = graph_inds[graph_ind] * type_inds[type_ind]
            predicted_points = pos_pred[relevant_inds]
            predicted_types_probs = types_prod[relevant_inds, type_ind]

            true_points = data.pos[relevant_inds]
            true_probs = data.x[relevant_inds, 0]

            num_inds = torch.sum(relevant_inds)
            relevant_likelihoods = []
            for atom_ind in range(num_inds):  # todo should we also punish it explicitly for probability outisde where we want it?
                relevant_likelihoods.extend(point_density(predicted_points[atom_ind], true_points) * predicted_types_probs[atom_ind])

            graphwise_probs[graph_ind].extend(relevant_likelihoods)

        graphwise_probs[graph_ind] = torch.stack(graphwise_probs[graph_ind])

    combined_scores = torch.cat(graphwise_probs)

    return combined_scores


def test_get_reconstruction_loss_old(data, decoding):
    score1 = get_reconstruction_likelihood_old(data, decoding, sigma=0.1)
    true_encoding = torch.cat((
        data.pos, F.one_hot(data.x[:, 0], num_classes=decoding.shape[1] - 3)
    ), dim=1)
    score2 = get_reconstruction_likelihood_old(data, true_encoding, sigma=0.1)

    assert score2.mean() / score1.mean() > 10


class point_cloud_encoder(nn.Module):
    def __init__(self, embedding_depth, num_layers, cutoff, seed, device, num_classes):
        super(point_cloud_encoder, self).__init__()

        self.device = device
        '''conditioning model'''
        torch.manual_seed(seed)

        self.conditioner = molecule_graph_model(
            num_atom_feats=1,
            num_mol_feats=0,
            output_dimension=embedding_depth,
            seed=seed,
            graph_convolution_type='TransformerConv',
            graph_aggregator='combo',
            concat_pos_to_atom_features=True,
            concat_mol_to_atom_features=False,
            concat_crystal_to_atom_features=False,
            activation='gelu',
            num_fc_layers=num_layers,
            fc_depth=embedding_depth,
            fc_norm_mode='layer',
            fc_dropout_probability=0,
            graph_node_norm='graph layer',
            graph_node_dropout=0,
            graph_message_norm=None,
            graph_message_dropout=0,
            num_attention_heads=num_layers,
            graph_message_depth=embedding_depth // 2,
            graph_node_dims=embedding_depth,
            num_graph_convolutions=num_layers,
            graph_embedding_depth=embedding_depth,
            nodewise_fc_layers=1,
            num_radial=50,
            radial_function='gaussian',
            max_num_neighbors=100,
            convolution_cutoff=cutoff,
            atom_type_embedding_dims=5,
            periodic_structure=False,
            outside_convolution_type='none'
        )

        # graph size model
        self.num_atoms_prediction = MLP(layers=1,
                                        filters=32,
                                        norm=None,
                                        dropout=0,
                                        input_dim=embedding_depth,
                                        output_dim=1,  # 3 extra dimensions for angle decoder
                                        conditioning_dim=0,  # include crystal information for the generator and the target packing coeff
                                        seed=seed,
                                        conditioning_mode=None,
                                        )

        self.composition_prediction = MLP(layers=1,
                                          filters=32,
                                          norm=None,
                                          dropout=0,
                                          input_dim=embedding_depth,
                                          output_dim=num_classes,  # 3 extra dimensions for angle decoder
                                          conditioning_dim=0,  # include crystal information for the generator and the target packing coeff
                                          seed=seed,
                                          conditioning_mode=None,
                                          )

    def forward(self, data):
        encoding = self.conditioner(data)
        num_atoms_prediction = self.num_atoms_prediction(encoding)
        composition_prediction = self.composition_prediction(encoding)

        return encoding, num_atoms_prediction, composition_prediction


class point_cloud_decoder(nn.Module):
    def __init__(self, embedding_depth, num_layers, cutoff, max_ntypes, seed, device):
        super(point_cloud_decoder, self).__init__()

        self.device = device

        self.conditioner = molecule_graph_model(
            num_atom_feats=embedding_depth,
            num_mol_feats=0,
            output_dimension=3 + max_ntypes,
            seed=seed,
            graph_convolution_type='TransformerConv',
            graph_aggregator=None,
            concat_pos_to_atom_features=True,
            concat_mol_to_atom_features=False,
            concat_crystal_to_atom_features=False,
            activation='gelu',
            num_fc_layers=0,
            graph_node_norm='graph layer',
            graph_node_dropout=0,
            graph_message_norm=None,
            graph_message_dropout=0,
            num_attention_heads=num_layers,
            graph_message_depth=embedding_depth // 2,
            graph_node_dims=embedding_depth,
            num_graph_convolutions=num_layers,
            graph_embedding_depth=embedding_depth,
            nodewise_fc_layers=1,
            num_radial=50,
            radial_function='gaussian',
            max_num_neighbors=100,
            convolution_cutoff=cutoff,
            atom_type_embedding_dims=1,
            periodic_structure=False,
            outside_convolution_type='none'
        )

    def forward(self, encoding, data, graph_sizes):
        """
        initialize nodes on randn with uniform embedding
        decode
        """
        data.pos = torch.randn_like(data.pos)
        data.x = encoding.repeat_interleave(torch.tensor(graph_sizes, dtype=torch.long, device=encoding.device), dim=0)
        data.x[:, 0] = 1  # to fool the encoder / so it won't throw wacky errors at us

        return self.conditioner(data)


def emd(x1, x2):
    d = cdist(x1, x2)
    assignment = linear_sum_assignment(d)  # min_weight_full_bipartite_matching(d)
    return d[assignment].sum() / min(len(x1), len(x2))
