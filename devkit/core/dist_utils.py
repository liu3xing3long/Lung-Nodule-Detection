'''
@Author: Jiamin Ren
@Date: 2020-04-29 09:56:57
'''
import os
import torch
import torch.multiprocessing as mp
import torch.distributed as dist

__all__ = [
    'init_dist','init_dist_slurm', 'broadcast_params','average_gradients']

def init_dist(backend='nccl',
              master_ip='127.0.0.1',
              port=29500):
    if mp.get_start_method(allow_none=True) is None:
        mp.set_start_method('spawn')
    os.environ['MASTER_ADDR'] = master_ip
    os.environ['MASTER_PORT'] = str(port)
    rank = int(os.environ['RANK'])
    world_size = int(os.environ['WORLD_SIZE'])
    num_gpus = torch.cuda.device_count()
    torch.cuda.set_device(rank % num_gpus)
    dist.init_process_group(backend=backend)
    return rank, world_size

def init_dist_slurm(backend='nccl', port=29500):
    if mp.get_start_method(allow_none=True) != 'spawn':
        mp.set_start_method('spawn')
    proc_id = int(os.environ['SLURM_PROCID'])
    ntasks = int(os.environ['SLURM_NTASKS'])
    node_list = os.environ['SLURM_NODELIST']
    num_gpus = torch.cuda.device_count()
    torch.cuda.set_device(proc_id%num_gpus)

    if '[' in node_list:
        beg = node_list.find('[')
        pos1 = node_list.find('-', beg)
        if pos1 < 0:
            pos1 = 1000
        pos2 = node_list.find(',', beg)
        if pos2 < 0:
            pos2 = 1000
        node_list = node_list[:min(pos1,pos2)].replace('[', '')
    addr = node_list[8:].replace('-', '.')

    os.environ['MASTER_PORT'] = str(port) 
    os.environ['MASTER_ADDR'] = addr
    os.environ['WORLD_SIZE'] = str(ntasks)
    os.environ['RANK'] = str(proc_id)
    dist.init_process_group(backend='nccl')

    rank = dist.get_rank()
    world_size = dist.get_world_size()
    return rank, world_size
    
def average_gradients(model):
    for param in model.parameters():
        if param.requires_grad and not (param.grad is None):
            dist.all_reduce(param.grad.data)

def broadcast_params(model):
    for p in model.state_dict().values():
        dist.broadcast(p, 0)

