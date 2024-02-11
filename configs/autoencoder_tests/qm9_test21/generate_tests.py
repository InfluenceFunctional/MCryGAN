from common.config_processing import load_yaml
import yaml
from copy import copy
import numpy as np

base_config = load_yaml('base.yaml')

# decoder layers, decoder points, weight decay, filter protons, positional noise, dropout, embedding_dim, bottleneck_dim, num_convs, num_nodewise, ramp_depth
config_list = [
    [4, 256, 0.05, True, 0, 0, 256, 256, 1, 4, True],  # 0 - converged
    [4, 256, 0.05, False, 0, 0, 256, 256, 1, 4, True],  # 1 - converged
    [4, 256, 0.05, True, 0, 0, 256, 256, 0, 4, True],  # 2  cancelled - flat
    [4, 256, 0.05, False, 0, 0, 256, 256, 0, 4, True],  # 3  cancelled - flat
    [4, 256, 0.05, True, 0, 0, 256, 256, 2, 2, True],  # 4 - converged - ~worse than 0
    [4, 256, 0.05, False, 0, 0, 256, 256, 2, 2, True],  # 5 - converged - ~worse than 1
    [4, 256, 0.1, True, 0, 0, 256, 256, 1, 4, True],  # 6 - converged
    [8, 256, 0.05, True, 0, 0, 256, 256, 1, 4, True],  # 7 - converged
    [4, 256, 0.05, True, 0, 0, 512, 512, 1, 4, True],  # 8 - converged
    [4, 256, 0.05, True, 0, 0, 256, 256, 1, 4, False],  # 9 - converged

]

np.random.seed(1)
ind = 0
for ix1 in range(len(config_list)):

    config = copy(base_config)
    config['logger']['run_name'] = config['logger']['run_name'] + '_' + str(ind)

    config['autoencoder']['model']['num_decoder_layers'] = config_list[ix1][0]
    config['autoencoder']['model']['num_decoder_points'] = config_list[ix1][1]
    config['autoencoder']['optimizer']['weight_decay'] = config_list[ix1][2]
    config['dataset']['filter_protons'] = config_list[ix1][3]
    config['autoencoder_positional_noise'] = config_list[ix1][4]
    config['autoencoder']['model']['graph_node_dropout'] = config_list[ix1][5]
    config['autoencoder']['model']['decoder_dropout_probability'] = config_list[ix1][5]
    config['autoencoder']['model']['embedding_depth'] = config_list[ix1][6]
    config['autoencoder']['model']['bottleneck_dim'] = config_list[ix1][7]
    config['autoencoder']['model']['num_graph_convolutions'] = config_list[ix1][8]
    config['autoencoder']['model']['nodewise_fc_layers'] = config_list[ix1][9]
    config['autoencoder']['model']['decoder_ramp_depth'] = config_list[ix1][10]

    with open(str(ind) + '.yaml', 'w') as outfile:
        yaml.dump(config, outfile, default_flow_style=False)

    ind += 1
