import os
import math
import argparse
import random
import logging
import imageio
import time
import pandas as pd
from copy import deepcopy

import torch
from torch.nn import functional as F
import torch.distributed as dist
import torch.multiprocessing as mp
from data.data_sampler import DistIterSampler

import options.options as option
from utils import util
from data.meta_learner import loader, create_dataloader, create_dataset, preprocessing
from models import create_model


def init_dist(backend='nccl', **kwargs):
    """initialization for distributed training"""
    if mp.get_start_method(allow_none=True) != 'spawn':
        mp.set_start_method('spawn')
    rank = int(os.environ['RANK'])
    num_gpus = torch.cuda.device_count()
    torch.cuda.set_device(rank % num_gpus)
    dist.init_process_group(backend=backend, **kwargs)


def main():
    #### options
    parser = argparse.ArgumentParser()
    parser.add_argument('-opt', type=str, help='Path to option YAML file.')
    parser.add_argument('--launcher', choices=['none', 'pytorch'], default='none',
                        help='job launcher')
    parser.add_argument('--local_rank', type=int, default=0)
    parser.add_argument('--exp_name', type=str, default='temp')
    parser.add_argument('--degradation_type', type=str, default=None)
    parser.add_argument('--sigma_x', type=float, default=None)
    parser.add_argument('--sigma_y', type=float, default=None)
    parser.add_argument('--theta', type=float, default=None)
    args = parser.parse_args()
    if args.exp_name == 'temp':
        opt = option.parse(args.opt, is_train=True)
    else:
        opt = option.parse(args.opt, is_train=True, exp_name=args.exp_name)

    # convert to NoneDict, which returns None for missing keys
    opt = option.dict_to_nonedict(opt)
    inner_loop_name = opt['train']['maml']['optimizer'][0] + str(opt['train']['maml']['adapt_iter']) + str(math.floor(math.log10(opt['train']['maml']['lr_alpha'])))
    meta_loop_name = opt['train']['optim'][0] + str(math.floor(math.log10(opt['train']['lr_G'])))

    if args.degradation_type is not None:
        if args.degradation_type == 'preset':
            opt['datasets']['val']['degradation_mode'] = args.degradation_type
        else:
            opt['datasets']['val']['degradation_type'] = args.degradation_type
    if args.sigma_x is not None:
        opt['datasets']['val']['sigma_x'] = args.sigma_x
    if args.sigma_y is not None:
        opt['datasets']['val']['sigma_y'] = args.sigma_y
    if args.theta is not None:
        opt['datasets']['val']['theta'] = args.theta
    if opt['datasets']['val']['degradation_mode'] == 'set':
        degradation_name = str(opt['datasets']['val']['degradation_type'])\
                  + '_' + str(opt['datasets']['val']['sigma_x']) \
                  + '_' + str(opt['datasets']['val']['sigma_y'])\
                  + '_' + str(opt['datasets']['val']['theta'])
    else:
        degradation_name = opt['datasets']['val']['degradation_mode']
    patch_name = 'p{}x{}'.format(opt['train']['maml']['patch_size'], opt['train']['maml']['num_patch']) if opt['train']['maml']['use_patch'] else 'full'
    use_real_flag = '_ideal' if opt['train']['use_real'] else ''
    folder_name = opt['name'] + '_' + degradation_name # + '_' + inner_loop_name + meta_loop_name + '_' + degradation_name + '_' + patch_name + use_real_flag

    if args.exp_name != 'temp':
        folder_name = args.exp_name

    #### distributed training settings
    if args.launcher == 'none':  # disabled distributed training
        opt['dist'] = False
        rank = -1
        print('Disabled distributed training.')
    else:
        opt['dist'] = True
        init_dist()
        world_size = torch.distributed.get_world_size()
        rank = torch.distributed.get_rank()

    #### loading resume state if exists
    if opt['path'].get('resume_state', None):
        # distributed resuming: all load into default GPU
        device_id = torch.cuda.current_device()
        resume_state = torch.load(opt['path']['resume_state'],
                                  map_location=lambda storage, loc: storage.cuda(device_id))
        option.check_resume(opt, resume_state['iter'])  # check resume options
    else:
        resume_state = None

    #### mkdir and loggers
    if rank <= 0:  # normal training (rank -1) OR distributed training (rank 0)
        if resume_state is None:
            #util.mkdir_and_rename(
            #    opt['path']['experiments_root'])  # rename experiment folder if exists
            #util.mkdirs((path for key, path in opt['path'].items() if not key == 'experiments_root'
            #             and 'pretrain_model' not in key and 'resume' not in key))
            if not os.path.exists(opt['path']['experiments_root']):
                os.mkdir(opt['path']['experiments_root'])
                # raise ValueError('Path does not exists - check path')

        # config loggers. Before it, the log will not work
        util.setup_logger('base', opt['path']['log'], 'train_' + opt['name'], level=logging.INFO,
                          screen=True, tofile=True)
        logger = logging.getLogger('base')
        #logger.info(option.dict2str(opt))
        # tensorboard logger
        if opt['use_tb_logger'] and 'debug' not in opt['name']:
            version = float(torch.__version__[0:3])
            if version >= 1.1:  # PyTorch 1.1
                from torch.utils.tensorboard import SummaryWriter
            else:
                logger.info(
                    'You are using PyTorch {}. Tensorboard will use [tensorboardX]'.format(version))
                from tensorboardX import SummaryWriter
            tb_logger = SummaryWriter(log_dir='../tb_logger/' + folder_name)
    else:
        util.setup_logger('base', opt['path']['log'], 'train', level=logging.INFO, screen=True)
        logger = logging.getLogger('base')

    #### random seed
    seed = opt['train']['manual_seed']
    if seed is None:
        seed = random.randint(1, 10000)
    if rank <= 0:
        logger.info('Random seed: {}'.format(seed))
    util.set_random_seed(seed)

    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True

    #### create train and val dataloader
    dataset_ratio = 200  # enlarge the size of each epoch
    for phase, dataset_opt in opt['datasets'].items():
        if phase == 'train':
            pass
        elif phase == 'val':
            if '+' in opt['datasets']['val']['name']:
                val_set, val_loader = [], []
                valname_list = opt['datasets']['val']['name'].split('+')
                for i in range(len(valname_list)):
                    val_set.append(create_dataset(dataset_opt, scale=opt['scale'],
                                                  kernel_size=opt['datasets']['train']['kernel_size'],
                                                  model_name=opt['network_E']['which_model_E'], idx=i))
                    val_loader.append(create_dataloader(val_set[-1], dataset_opt, opt, None))
            else:
                val_set = create_dataset(dataset_opt, scale=opt['scale'],
                                         kernel_size=opt['datasets']['train']['kernel_size'],
                                         model_name=opt['network_E']['which_model_E'])
                # val_set = loader.get_dataset(opt, train=False)
                val_loader = create_dataloader(val_set, dataset_opt, opt, None)
            if rank <= 0:
                logger.info('Number of val images in [{:s}]: {:d}'.format(
                    dataset_opt['name'], len(val_set)))
        else:
            raise NotImplementedError('Phase [{:s}] is not recognized.'.format(phase))

    #### create model
    models = create_model(opt)
    assert len(models) == 2
    model, est_model = models[0], models[1]
    modelcp, est_modelcp = create_model(opt)
    _, est_model_fixed = create_model(opt)

    center_idx = (opt['datasets']['val']['N_frames']) // 2
    lr_alpha = opt['train']['maml']['lr_alpha']
    update_step = opt['train']['maml']['adapt_iter']

    pd_log = pd.DataFrame(columns=['PSNR_Bicubic', 'PSNR_Ours', 'SSIM_Bicubic', 'SSIM_Ours'])

    def crop(LR_seq, HR, num_patches_for_batch=4, patch_size=44):
        """
        Crop given patches.

        Args:
            LR_seq: (B=1) x T x C x H x W
            HR: (B=1) x C x H x W

            patch_size (int, optional):

        Return:
            B(=batch_size) x T x C x H x W
        """
        # Find the lowest resolution
        cropped_lr = []
        cropped_hr = []
        assert HR.size(0) == 1
        LR_seq_ = LR_seq[0]
        HR_ = HR[0]
        for _ in range(num_patches_for_batch):
            patch_lr, patch_hr = preprocessing.common_crop(LR_seq_, HR_, patch_size=patch_size // 2)
            cropped_lr.append(patch_lr)
            cropped_hr.append(patch_hr)

        cropped_lr = torch.stack(cropped_lr, dim=0)
        cropped_hr = torch.stack(cropped_hr, dim=0)

        return cropped_lr, cropped_hr
    '''
    if opt['dist']:
        # multi-GPU testing
        # PSNR_rlt: psnr_init, psnr_before, psnr_after
        psnr_rlt = [{}, {}, {}]
        # SSIM_rlt: ssim_init, ssim_after
        ssim_rlt = [{}, {}]

        if rank == 0:
            pbar = util.ProgressBar(len(val_set))
        for idx in range(rank, len(val_set), world_size):
            val_data = val_set[idx]
            if 'name' in val_data.keys():
                name = val_data['name'][center_idx][0]
            else:
                name = '{}/{:08d}'.format(val_data['folder'], int(val_data['idx'].split('/')[0]))

            train_folder = os.path.join('../results', folder_name, name)
            if not os.path.isdir(train_folder):
                os.makedirs(train_folder, exist_ok=True)

            val_data['SuperLQs'].unsqueeze_(0)
            val_data['LQs'].unsqueeze_(0)
            val_data['GT'].unsqueeze_(0)
            folder = val_data['folder']
            idx_d, max_idx = val_data['idx'].split('/')
            idx_d, max_idx = int(idx_d), int(max_idx)
            for i in range(len(psnr_rlt)):
                if psnr_rlt[i].get(folder, None) is None:
                    psnr_rlt[i][folder] = torch.zeros(max_idx, dtype=torch.float32, device='cuda')
            for i in range(len(ssim_rlt)):
                if ssim_rlt[i].get(folder, None) is None:
                    ssim_rlt[i][folder] = torch.zeros(max_idx, dtype=torch.float32, device='cuda')

            cropped_meta_train_data = {}
            meta_train_data = {}
            meta_test_data = {}

            # Make SuperLR seq using estimation model
            meta_train_data['GT'] = val_data['LQs'][:, center_idx]
            meta_test_data['LQs'] = val_data['LQs'][0:1]
            meta_test_data['GT'] = val_data['GT'][0:1, center_idx]
            # Check whether the batch size of each validation data is 1
            assert val_data['SuperLQs'].size(0) == 1

            #modelcp.netG = deepcopy(model.netG)
            modelcp.netG, est_modelcp.netE = deepcopy(model.netG), deepcopy(est_model.netE)

            optim_params = []
            for k, v in modelcp.netG.named_parameters():
                if v.requires_grad:
                    optim_params.append(v)
            for k, v in est_modelcp.netE.named_parameters():
                if v.requires_grad:
                    optim_params.append(v)
            if opt['train']['maml']['optimizer'] == 'Adam':
                inner_optimizer = torch.optim.Adam(optim_params, lr=lr_alpha,
                                                  betas=(
                                                  opt['train']['maml']['beta1'], opt['train']['maml']['beta2']))
            elif opt['train']['maml']['optimizer'] == 'SGD':
                inner_optimizer = torch.optim.SGD(optim_params, lr=lr_alpha)
            else:
                raise NotImplementedError()

            psnr_rlt[0][folder][idx_d] = 0.1
            ssim_rlt[0][folder][idx_d] = 0.1

            #### Forward
            # Before (After Meta update, Before Inner update)
            modelcp.feed_data(meta_test_data)
            modelcp.test()
            model_start_visuals = modelcp.get_current_visuals(need_GT=True)
            hr_image = util.tensor2img(model_start_visuals['GT'], mode='rgb')
            start_image = util.tensor2img(model_start_visuals['rlt'], mode='rgb')
            imageio.imwrite(os.path.join(train_folder, 'sr_start.png'), start_image)
            psnr_rlt[1][folder][idx_d] = util.calculate_psnr(start_image, hr_image)
            # Inner Loop Update
            st = time.time()
            for i in range(update_step):

                # Make SuperLR seq using UPDATED estimation model
                if not opt['train']['use_real']:
                    est_modelcp.feed_data(val_data)
                    #est_model.test()
                    est_modelcp.forward_without_optim()
                    superlr_seq = est_modelcp.fake_L
                    meta_train_data['LQs'] = superlr_seq
                else:
                    meta_train_data['LQs'] = val_data['SuperLQs']

                # Update both modelcp + estmodelcp jointly
                inner_optimizer.zero_grad()
                if opt['train']['maml']['use_patch']:
                    cropped_meta_train_data['LQs'], cropped_meta_train_data['GT'] = \
                        crop(meta_train_data['LQs'], meta_train_data['GT'],
                            opt['train']['maml']['num_patch'],
                            opt['train']['maml']['patch_size'])
                    modelcp.feed_data(cropped_meta_train_data)
                else:
                    modelcp.feed_data(meta_train_data)

                loss_train = modelcp.calculate_loss()
                loss_train.backward()
                inner_optimizer.step()

            et = time.time()
            update_time = et - st

            modelcp.feed_data(meta_test_data)
            modelcp.test()
            model_update_visuals = modelcp.get_current_visuals(need_GT=False)
            update_image = util.tensor2img(model_update_visuals['rlt'], mode='rgb')
            # Save and calculate final image
            imageio.imwrite(os.path.join(train_folder, 'sr_finish.png'), update_image)
            psnr_rlt[2][folder][idx_d] = util.calculate_psnr(update_image, hr_image)
            #ssim_rlt[1][folder][idx_d] = util.calculate_ssim(update_image, hr_image)

            if name in pd_log.index:
                pd_log.at[name, 'PSNR_Init'] = psnr_rlt[0][folder][idx_d].item()
                pd_log.at[name, 'PSNR_Start'] = (psnr_rlt[1][folder][idx_d] - psnr_rlt[0][folder][idx_d]).item()
                pd_log.at[name, 'PSNR_Final({})'.format(update_step)] = (psnr_rlt[2][folder][idx_d] - psnr_rlt[0][folder][idx_d]).item()
                pd_log.at[name, 'SSIM_Init'] = ssim_rlt[0][folder][idx_d].item()
                pd_log.at[name, 'SSIM_Final'] = ssim_rlt[1][folder][idx_d].item()
            else:
                pd_log.loc[name] = [psnr_rlt[0][folder][idx_d].item(),
                                    psnr_rlt[1][folder][idx_d].item() - psnr_rlt[0][folder][idx_d].item(),
                                    psnr_rlt[2][folder][idx_d].item() - psnr_rlt[0][folder][idx_d].item(),
                                    ssim_rlt[0][folder][idx_d].item(), ssim_rlt[1][folder][idx_d].item()]

            pd_log.to_csv(os.path.join('../results', folder_name, 'psnr_update.csv'))

            del modelcp.netG, est_modelcp.netE
            if rank == 0:
                for _ in range(world_size):
                    pbar.update('Test {} - {}/{}: I: {:.3f}/{:.4f} \tF+: {:.3f}/{:.4f} \tTime: {:.3f}s'
                                .format(folder, idx_d, max_idx,
                                        psnr_rlt[0][folder][idx_d].item(), ssim_rlt[0][folder][idx_d].item(),
                                        psnr_rlt[2][folder][idx_d].item(), ssim_rlt[1][folder][idx_d].item(),
                                        update_time
                                        ))

        ## collect data
        for i in range(len(psnr_rlt)):
            for _, v in psnr_rlt[i].items():
                dist.reduce(v, 0)
        for i in range(len(ssim_rlt)):
            for _, v in ssim_rlt[i].items():
                dist.reduce(v, 0)
        dist.barrier()

        if rank == 0:
            psnr_rlt_avg = {}
            psnr_total_avg = [0., 0., 0.]
            # 0: Init, 1: Start, 2: Final
            #Just calculate the final value of psnr_rlt(i.e. psnr_rlt[2])
            for k, v_init in psnr_rlt[0].items():
                v_start = psnr_rlt[1][k]
                v_final = psnr_rlt[2][k]
                psnr_rlt_avg[k] = [torch.sum(v_init).cpu().item() / (v_init!=0).sum().item(),
                                     torch.sum(v_start).cpu().item() / (v_start!=0).sum().item(),
                                     torch.sum(v_final).cpu().item() / (v_final!=0).sum().item()]
                for i in range(len(psnr_rlt)):
                    psnr_total_avg[i] += psnr_rlt_avg[k][i]
            for i in range(len(psnr_rlt)):
                psnr_total_avg[i] /= len(psnr_rlt[0])
            log_s = '# Validation # Final PSNR: {:.4e}:'.format(psnr_total_avg[2])
            for k, v in psnr_rlt_avg.items():
                log_s += ' {}: {:.4e}'.format(k, v[2])
            logger.info(log_s)

            ssim_rlt_avg = {}
            ssim_total_avg = 0.
            #Just calculate the final value of ssim_rlt(i.e. ssim_rlt[1])
            for k, v in ssim_rlt[1].items():
                ssim_rlt_avg[k] = torch.sum(v).cpu().item() / (v!=0).sum().item()
                ssim_total_avg += ssim_rlt_avg[k]
            ssim_total_avg /= len(ssim_rlt[1])
            log_s = '# Validation # SSIM: {:.4e}:'.format(ssim_total_avg)
            for k, v in ssim_rlt_avg.items():
                log_s += ' {}: {:.4e}'.format(k, v)
            logger.info(log_s)

        termination = True

    else:
    '''
    # Single GPU
    # PSNR_rlt: psnr_init, psnr_before, psnr_after
    psnr_rlt = [{}, {}]
    # SSIM_rlt: ssim_init, ssim_after
    ssim_rlt = [{}, {}]
    pbar = util.ProgressBar(len(val_set))
    for val_data in val_loader:
        folder = val_data['folder'][0]
        idx_d = int(val_data['idx'][0].split('/')[0])
        if 'name' in val_data.keys():
            name = val_data['name'][0][center_idx][0]
        else:
            #name = '{}/{:08d}'.format(folder, idx_d)
            name = folder

        train_folder = os.path.join('../results_for_paper', folder_name, name)

        hr_train_folder = os.path.join(train_folder, 'hr')
        bic_train_folder = os.path.join(train_folder, 'bic')
        maml_train_folder = os.path.join(train_folder, 'maml')
        #slr_train_folder = os.path.join(train_folder, 'slr')

        # print(train_folder)
        if not os.path.exists(train_folder):
            os.makedirs(train_folder, exist_ok=False)
        if not os.path.exists(hr_train_folder):
            os.mkdir(hr_train_folder)
        if not os.path.exists(bic_train_folder):
            os.mkdir(bic_train_folder)
        if not os.path.exists(maml_train_folder):
            os.mkdir(maml_train_folder)
        #if not os.path.exists(slr_train_folder):
        #    os.mkdir(slr_train_folder)

        for i in range(len(psnr_rlt)):
            if psnr_rlt[i].get(folder, None) is None:
                psnr_rlt[i][folder] = []
        for i in range(len(ssim_rlt)):
            if ssim_rlt[i].get(folder, None) is None:
                ssim_rlt[i][folder] = []
        
        if idx_d % 10 != 5:
            #continue
            pass

        cropped_meta_train_data = {}
        meta_train_data = {}
        meta_test_data = {}

        # Make SuperLR seq using estimation model
        meta_train_data['GT'] = val_data['LQs'][:, center_idx]
        meta_test_data['LQs'] = val_data['LQs'][0:1]
        meta_test_data['GT'] = val_data['GT'][0:1, center_idx]
        # Check whether the batch size of each validation data is 1
        assert val_data['SuperLQs'].size(0) == 1

        if opt['network_G']['which_model_G'] == 'TOF':
            LQs = meta_test_data['LQs']
            B, T, C, H, W = LQs.shape
            LQs = LQs.reshape(B*T, C, H, W)
            Bic_LQs = F.interpolate(LQs, scale_factor=opt['scale'], mode='bicubic', align_corners=True)
            meta_test_data['LQs'] = Bic_LQs.reshape(B, T, C, H*opt['scale'], W*opt['scale'])
        '''
        ## Before start training, first save the bicubic, real outputs
        # Bicubic
        modelcp.load_network(opt['path']['bicubic_G'], modelcp.netG)
        modelcp.feed_data(meta_test_data)
        modelcp.test()
        model_start_visuals = modelcp.get_current_visuals(need_GT=True)
        hr_image = util.tensor2img(model_start_visuals['GT'], mode='rgb')
        start_image = util.tensor2img(model_start_visuals['rlt'], mode='rgb')
        #imageio.(os.path.join(train_folder, 'hr.png'), hr_image)
        #imageio.imwrite(os.path.join(train_folder, 'sr_1bicubic.png'), start_image)
        '''
        model.feed_data(meta_test_data)
        model.test()
        model_start_visuals = model.get_current_visuals(need_GT=True)
        hr_image = util.tensor2img(model_start_visuals['GT'], mode='rgb')
        start_image = util.tensor2img(model_start_visuals['rlt'], mode='rgb')
        #####imageio.imwrite(os.path.join(hr_train_folder, '{:08d}.png'.format(idx_d)), hr_image)
        #####imageio.imwrite(os.path.join(bic_train_folder, '{:08d}.png'.format(idx_d)), start_image)
        psnr_rlt[0][folder].append(util.calculate_psnr(start_image, hr_image))
        #ssim_rlt[0][folder].append(util.calculate_ssim(start_image, hr_image)) #.append(0.1)
        ssim_rlt[0][folder].append(0.2)
        # modelcp.netG = deepcopy(model.netG)
        modelcp.netG, est_modelcp.netE = deepcopy(model.netG), deepcopy(est_model.netE)

        ########## SLR LOSS Preparation ############
        est_model_fixed.load_network(opt['path']['fixed_E'], est_model_fixed.netE)

        optim_params = []
        for k, v in modelcp.netG.named_parameters():
            if v.requires_grad:
                optim_params.append(v)
        
        
        if not opt['train']['use_real']:
            for k, v in est_modelcp.netE.named_parameters():
                if v.requires_grad:
                    optim_params.append(v)
        
        if opt['train']['maml']['optimizer'] == 'Adam':
            inner_optimizer = torch.optim.Adam(optim_params, lr=lr_alpha,
                                               betas=(
                                                   opt['train']['maml']['beta1'],
                                                   opt['train']['maml']['beta2']))
        elif opt['train']['maml']['optimizer'] == 'SGD':
            inner_optimizer = torch.optim.SGD(optim_params, lr=lr_alpha)
        else:
            raise NotImplementedError()

        '''
        #### Forward
        # Before (After Meta update, Before Inner update)
        # modelcp.feed_data(meta_test_data)
        # modelcp.test()
        # model_start_visuals = modelcp.get_current_visuals(need_GT=True)
        # hr_image = util.tensor2img(model_start_visuals['GT'], mode='rgb')
        # start_image = util.tensor2img(model_start_visuals['rlt'], mode='rgb')
        # imageio.imwrite(os.path.join(train_folder, 'hr.png'), hr_image)
        # imageio.imwrite(os.path.join(train_folder, 'sr_basereal.png'), start_image)
        # psnr_rlt[1][folder].append(util.calculate_psnr(start_image, hr_image))
        # psnr_rlt[1][folder].append(0)
        '''
        # Inner Loop Update
        st = time.time()
        for i in range(update_step):
            # Make SuperLR seq using UPDATED estimation model
            if not opt['train']['use_real']:
                est_modelcp.feed_data(val_data)
                # est_model.test()
                est_modelcp.forward_without_optim()
                superlr_seq = est_modelcp.fake_L
                meta_train_data['LQs'] = superlr_seq
            else:
                meta_train_data['LQs'] = val_data['SuperLQs']

            if opt['network_G']['which_model_G'] == 'TOF':
                # Bicubic upsample to match the size
                LQs = meta_train_data['LQs']
                B, T, C, H, W = LQs.shape
                LQs = LQs.reshape(B*T, C, H, W)
                Bic_LQs = F.interpolate(LQs, scale_factor=opt['scale'], mode='bicubic', align_corners=True)
                meta_train_data['LQs'] = Bic_LQs.reshape(B, T, C, H*opt['scale'], W*opt['scale'])

            # Update both modelcp + estmodelcp jointly
            inner_optimizer.zero_grad()
            if opt['train']['maml']['use_patch']:
                cropped_meta_train_data['LQs'], cropped_meta_train_data['GT'] = \
                    crop(meta_train_data['LQs'], meta_train_data['GT'],
                         opt['train']['maml']['num_patch'],
                         opt['train']['maml']['patch_size'])
                modelcp.feed_data(cropped_meta_train_data)
            else:
                modelcp.feed_data(meta_train_data)

            loss_train = modelcp.calculate_loss()
            
            ##################### SLR LOSS ###################
            est_model_fixed.feed_data(val_data)
            est_model_fixed.test()
            slr_initialized = est_model_fixed.fake_L
            slr_initialized = slr_initialized.to('cuda')
            if opt['network_G']['which_model_G'] == 'TOF':
                loss_train += 10 * F.l1_loss(LQs.to('cuda').squeeze(0), slr_initialized)
            else:
                loss_train += 10 * F.l1_loss(meta_train_data['LQs'].to('cuda'), slr_initialized)
            
            loss_train.backward()
            inner_optimizer.step()

        et = time.time()
        update_time = et - st

        modelcp.feed_data(meta_test_data)
        modelcp.test()
        '''
        # Save SLR image
        est_modelcp.feed_data(val_data)
        est_modelcp.test()
        est_model_visuals = est_modelcp.get_current_visuals(need_GT=False)
        slr_image = util.tensor2img(est_model_visuals['rlt'], mode='rgb')
        imageio.imwrite(os.path.join(slr_train_folder, '{:08d}.png'.format(idx_d)), slr_image)
        '''
        model_update_visuals = modelcp.get_current_visuals(need_GT=False)
        update_image = util.tensor2img(model_update_visuals['rlt'], mode='rgb')
        # Save and calculate final image
        #imageio.imwrite(os.path.join(train_folder, 'sr_ours.png'), update_image)
        #####imageio.imwrite(os.path.join(maml_train_folder, '{:08d}.png'.format(idx_d)), update_image)
        psnr_rlt[1][folder].append(util.calculate_psnr(update_image, hr_image))
        #ssim_rlt[1][folder].append(util.calculate_ssim(update_image, hr_image))
        ssim_rlt[1][folder].append(0.1)
        name_df = '{}/{:08d}'.format(folder, idx_d)
        if name_df in pd_log.index:
            pd_log.at[name_df, 'PSNR_Bicubic'] = psnr_rlt[0][folder][-1]
            pd_log.at[name_df, 'PSNR_Ours'] = psnr_rlt[1][folder][-1]
            pd_log.at[name_df, 'SSIM_Bicubic'] = ssim_rlt[0][folder][-1]
            pd_log.at[name_df, 'SSIM_Ours'] = ssim_rlt[1][folder][-1]
        else:
            pd_log.loc[name_df] = [psnr_rlt[0][folder][-1],
                                psnr_rlt[1][folder][-1],
                                ssim_rlt[0][folder][-1], ssim_rlt[1][folder][-1]]

        pd_log.to_csv(os.path.join('../results_for_paper', folder_name, 'psnr_update.csv'))

        pbar.update('Test {} - {}: I: {:.3f}/{:.4f} \tF+: {:.3f}/{:.4f} \tTime: {:.3f}s'
                        .format(folder, idx_d,
                                psnr_rlt[0][folder][-1], ssim_rlt[0][folder][-1],
                                psnr_rlt[1][folder][-1], ssim_rlt[1][folder][-1],
                                update_time
                                ))

    psnr_rlt_avg = {}
    psnr_total_avg = 0.
    # Just calculate the final value of psnr_rlt(i.e. psnr_rlt[2])
    for k, v in psnr_rlt[0].items():
        psnr_rlt_avg[k] = sum(v) / len(v)
        psnr_total_avg += psnr_rlt_avg[k]
    psnr_total_avg /= len(psnr_rlt[0])
    log_s = '# Validation # Bic PSNR: {:.4e}:'.format(psnr_total_avg)
    for k, v in psnr_rlt_avg.items():
        log_s += ' {}: {:.4e}'.format(k, v)
    logger.info(log_s)

    psnr_rlt_avg = {}
    psnr_total_avg = 0.
    # Just calculate the final value of psnr_rlt(i.e. psnr_rlt[2])
    for k, v in psnr_rlt[1].items():
        psnr_rlt_avg[k] = sum(v) / len(v)
        psnr_total_avg += psnr_rlt_avg[k]
    psnr_total_avg /= len(psnr_rlt[1])
    log_s = '# Validation # PSNR: {:.4e}:'.format(psnr_total_avg)
    for k, v in psnr_rlt_avg.items():
        log_s += ' {}: {:.4e}'.format(k, v)
    logger.info(log_s)

    ssim_rlt_avg = {}
    ssim_total_avg = 0.
    # Just calculate the final value of ssim_rlt(i.e. ssim_rlt[1])
    for k, v in ssim_rlt[0].items():
        ssim_rlt_avg[k] = sum(v) / len(v)
        ssim_total_avg += ssim_rlt_avg[k]
    ssim_total_avg /= len(ssim_rlt[0])
    log_s = '# Validation # Bicubic SSIM: {:.4e}:'.format(ssim_total_avg)
    for k, v in ssim_rlt_avg.items():
        log_s += ' {}: {:.4e}'.format(k, v)
    logger.info(log_s)

    ssim_rlt_avg = {}
    ssim_total_avg = 0.
    # Just calculate the final value of ssim_rlt(i.e. ssim_rlt[1])
    for k, v in ssim_rlt[1].items():
        ssim_rlt_avg[k] = sum(v) / len(v)
        ssim_total_avg += ssim_rlt_avg[k]
    ssim_total_avg /= len(ssim_rlt[1])
    log_s = '# Validation # SSIM: {:.4e}:'.format(ssim_total_avg)
    for k, v in ssim_rlt_avg.items():
        log_s += ' {}: {:.4e}'.format(k, v)
    logger.info(log_s)

    logger.info('End of evaluation.')

if __name__ == '__main__':
    main()
