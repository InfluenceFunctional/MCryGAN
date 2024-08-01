import numpy as np
from tqdm import tqdm
import torch
import lmdb
import pickle
import gzip
import multiprocessing as mp
import os
import rdkit.Chem as Chem
from rdkit.Chem import AllChem
from pathlib import Path

from mxtaltools.common.utils import chunkify
from mxtaltools.dataset_management.CrystalData import CrystalData



def process_smiles_list(lines, chunk_ind, file_ind, chunk_ind2):

    samples = [process_smiles(line) for line in lines]
    samples = [sample for sample in samples if sample is not None]

    with open(fr'D:\\crystal_datasets\\zinc22\\chunks\chunk_{chunk_ind}_{file_ind}_{chunk_ind2}.pkl', 'wb') as handle:
        pickle.dump(samples, handle, protocol=pickle.HIGHEST_PROTOCOL)

    # sample_inds = [overall_index + cc_idx for cc_idx in range(1, len(samples) + 1)]
    # data_dict = {str(k): v for k, v in zip(sample_inds, samples)}
    # overall_index += len(samples)
    #
    # map_size = write_lmdb(database_path, map_size, data_dict)
    #del data_dict, samples

    del samples


def process_smiles(line):
    try:
        mol = Chem.MolFromSmiles(line)
        mol2 = Chem.AddHs(mol)
        AllChem.EmbedMolecule(mol2)
        conf = mol2.GetConformer()
    except:
        return None

    coords = np.array(conf.GetPositions())
    atom_types = [atom.GetAtomicNum() for atom in mol2.GetAtoms()]

    # molecule sizes filter
    if len(atom_types) < 6 or len(atom_types) > 100:
        return None

    # atom types filter
    if not set(atom_types).issubset([1, 5, 6, 7, 8, 9, 14, 15, 16, 17, 35, 53]):
        return None

    sample = CrystalData(
        x=torch.tensor(atom_types, dtype=torch.long),
        pos=torch.tensor(coords, dtype=torch.float32),
        smiles=Chem.MolToSmiles(mol),
        identifier=mol.GetProp("_Name"),
        y=torch.zeros(1, dtype=torch.float32),
        require_crystal_features=False,
    )

    # molecule radius filter
    if sample.radius > 15:
        return None

    return sample.to_dict()


if __name__ == '__main__':
    parent_directory = r'D:\crystal_datasets\zinc22'
    chunks_dir = os.path.join(Path(parent_directory), 'chunks')
    #parent_directory = r'/vast/mk8347/zinc'

    lmdb_database = 'zinc.lmdb'
    map_size = int(1e9)  # map size in bytes

    os.chdir(parent_directory)
    dirs = os.listdir()

    # if not os.path.exists(lmdb_database.split('.lmdb')[0] + '_keys.npy'):
    #     overall_index = int(0)
    # else:
    #     overall_index = np.load(lmdb_database.split('.lmdb')[0] + '_keys.npy', allow_pickle=True).item()

    keys_path = parent_directory + '/' + lmdb_database.split('.lmdb')[0] + '_keys'
    database_path = parent_directory + '/' + lmdb_database
    chunk_ind = - 1
    min_chunk = 0
    max_chunk = min(100000, len(dirs))
    tot_index = 0

    pool = mp.Pool(mp.cpu_count() - 1)

    with tqdm(total=max_chunk) as pbar:
        while chunk_ind < max_chunk - 1:
            pbar.update(1)
            chunk_ind += 1

            if not (max_chunk > chunk_ind >= min_chunk):
                continue

            if dirs[chunk_ind][0] == 'H':
                dirpath = Path(dirs[chunk_ind])
                for file_ind, file in enumerate(tqdm(os.listdir(dirpath))):
                    chunkpath = os.path.join(chunks_dir, fr'chunk_{chunk_ind}_{file_ind}.pkl')
                    if not os.path.exists(chunkpath):
                        filepath = Path(file)
                        combo_path = os.path.join(dirpath, filepath)

                        if combo_path[-3:] == '.gz':
                            with gzip.open(combo_path, 'r') as f:
                                lines = f.readlines()
                        elif combo_path[-4:] == '.smi':
                            with open(combo_path, 'r') as f:
                                lines = f.readlines()
                        else:
                            pass

                        chunks = chunkify(lines, int(np.ceil(len(lines) / 1000)))
                        del lines

                        for chunk_ind2, chunk in enumerate(chunks):
                            pool.apply_async(process_smiles_list, args=(chunk, chunk_ind, file_ind, chunk_ind2))

    pool.close()
    pool.join()

