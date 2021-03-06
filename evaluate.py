'''
Author: Xingtong Liu, Ayushi Sinha, Masaru Ishii, Gregory D. Hager, Austin Reiter, Russell H. Taylor, and Mathias Unberath

Copyright (C) 2019 Johns Hopkins University - All Rights Reserved
You may use, distribute and modify this code under the
terms of the GNU GENERAL PUBLIC LICENSE Version 3 license for non-commercial usage.

You should have received a copy of the GNU GENERAL PUBLIC LICENSE Version 3 license with
this file. If not, please write to: xliu89@jh.edu or rht@jhu.edu or unberath@jhu.edu
'''

import tqdm
import cv2
import numpy as np
from pathlib import Path
import torchsummary
import torch
import random
from tensorboardX import SummaryWriter
import albumentations as albu
import argparse
# Local
import models
import losses
import utils
import dataset

if __name__ == '__main__':
    cv2.destroyAllWindows()
    parser = argparse.ArgumentParser(
        description='Self-supervised Depth Estimation on Monocular Endoscopy Dataset--Evaluation',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--downsampling', type=float, default=4.0,
                        help='image downsampling rate to speed up training and reduce overfitting')
    parser.add_argument('--torchsummary_input_size', nargs='+', type=int,
                        help='input size for torchsummary (analysis purpose only)')
    parser.add_argument('--batch_size', type=int, default=8, help='batch size for testing')
    parser.add_argument('--num_workers', type=int, default=8, help='number of workers for input data loader')
    parser.add_argument('--teacher_depth', type=int, default=7, help='depth of teacher model')
    parser.add_argument('--filter_base', type=int, default=3, help='filter base of teacher model')
    parser.add_argument('--inlier_percentage', type=float, default=0.995,
                        help='percentage of inliers of SfM point clouds (for pruning some outliers)')
    parser.add_argument('--testing_patient_id', type=int, help='id of the testing patient')
    parser.add_argument('--load_intermediate_data', action='store_true', help='whether to load intermediate data')
    parser.add_argument('--visualize_dataset_input', action='store_true',
                        help='whether to visualize input of data loader')
    parser.add_argument('--use_hsv_colorspace', action='store_true',
                        help='convert RGB to hsv colorspace')
    parser.add_argument('--training_root', type=str, help='root of the training input and ouput')
    parser.add_argument('--architecture_summary', action='store_true', help='display the network architecture')
    parser.add_argument('--student_model_path', type=str, default=None, help='path to the trained student model')

    args = parser.parse_args()

    # Fix randomness for reproducibility
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    torch.manual_seed(10085)
    np.random.seed(10085)
    random.seed(10085)
    device = torch.device("cuda")

    # Hyper-parameters
    downsampling = args.downsampling
    height, width = args.torchsummary_input_size
    batch_size = args.batch_size
    num_workers = args.num_workers
    teacher_depth = args.teacher_depth
    filter_base = args.filter_base
    inlier_percentage = args.inlier_percentage
    which_bag = args.testing_patient_id
    load_intermediate_data = args.load_intermediate_data
    visualize = args.visualize_dataset_input
    is_hsv = args.use_hsv_colorspace
    training_root = args.training_root
    display_architecture = args.architecture_summary
    teacher_model_path = args.teacher_model_path
    best_student_model_path = args.student_model_path

    depth_estimation_model_teacher = []
    failure_sequences = []

    test_transforms = albu.Compose([
        albu.Normalize(mean=(0.5, 0.5, 0.5), std=(0.5, 0.5, 0.5), max_pixel_value=255.0, p=1.)], p=1.)

    root = Path(training_root) / 'down_{down}_depth_{depth}_base_{base}_inliner_{inlier}_hsv_{hsv}_bag_{bag}'.format(
        bag=which_bag,
        down=downsampling,
        depth=teacher_depth,
        base=filter_base,
        inlier=inlier_percentage,
        hsv=is_hsv)

    writer = SummaryWriter(log_dir=str(root / "runs"))
    data_root = root / "data"
    try:
        data_root.mkdir(mode=0o777, parents=True)
    except OSError:
        pass
    precompute_root = root / "precompute"
    try:
        precompute_root.mkdir(mode=0o777, parents=True)
    except OSError:
        pass

    # Read initial pose information
    frame_index_array, translation_dict, rotation_dict = utils.read_initial_pose_file(
        str(data_root / ("bag_" + str(which_bag)) / ("initial_poses_patient_" + str(which_bag) + ".txt")))
    # Get color image filenames
    test_filenames = utils.get_filenames_from_frame_indexes(data_root / ("bag_" + str(which_bag)), frame_index_array)

    training_folder_list, val_folder_list = utils.get_parent_folder_names(data_root, which_bag=which_bag)

    test_dataset = dataset.SfMDataset(image_file_names=test_filenames,
                                      folder_list=training_folder_list + val_folder_list,
                                      to_augment=True,
                                      transform=test_transforms,
                                      downsampling=downsampling,
                                      net_depth=teacher_depth, inlier_percentage=inlier_percentage,
                                      use_store_data=load_intermediate_data,
                                      store_data_root=precompute_root,
                                      use_view_indexes_per_point=True, visualize=visualize,
                                      phase="test", is_hsv=is_hsv)
    test_loader = torch.utils.data.DataLoader(dataset=test_dataset, batch_size=batch_size, shuffle=False,
                                              num_workers=batch_size)

    # Directories for storing models and results
    model_root = root / "models"
    try:
        model_root.mkdir(mode=0o777, parents=True)
    except OSError:
        pass
    evaluation_root = root / "evaluation"
    try:
        evaluation_root.mkdir(mode=0o777, parents=True)
    except OSError:
        pass

    if best_student_model_path is None:
        best_student_model_path = model_root / "best_student_model.pt"

    depth_estimation_model_student = models.FCDenseNet57(n_classes=1)
    # Initialize the depth estimation network with Kaiming He initialization
    utils.init_net(depth_estimation_model_student, type="kaiming", mode="fan_in", activation_mode="relu",
                   distribution="normal")
    # Multi-GPU running
    depth_estimation_model_student = torch.nn.DataParallel(depth_estimation_model_student)
    # Summary network architecture
    if display_architecture:
        torchsummary.summary(depth_estimation_model_student, input_size=(3, height, width))

    # Load previous student model
    state = {}
    if best_student_model_path.exists():
        print("Loading {:s} ...".format(str(best_student_model_path)))
        state = torch.load(str(best_student_model_path))
        step = state['step']
        epoch = state['epoch']
        depth_estimation_model_student.load_state_dict(state['model'])
        print('Restored model, epoch {}, step {}'.format(epoch, step))
    else:
        print("Student model could not be found")
        raise OSError

    # Set model to evaluation mode
    depth_estimation_model_student.eval()
    for param in depth_estimation_model_student.parameters():
        param.requires_grad = False

    # Update progress bar
    tq = tqdm.tqdm(total=len(test_loader) * batch_size)
    try:
        for batch, (colors_1, boundaries, intrinsic_matrices,
                    image_names) in enumerate(test_loader):
            colors_1, boundaries, intrinsic_matrices = \
                colors_1.to(device), boundaries.to(device), intrinsic_matrices.to(device)
            tq.update(batch_size)
            colors_1 = boundaries * colors_1
            predicted_depth_maps_1 = depth_estimation_model_student(colors_1)
            utils.write_test_output_with_initial_pose(evaluation_root, colors_1, torch.abs(predicted_depth_maps_1), boundaries,
                                                      intrinsic_matrices, is_hsv,
                                                      image_names,
                                                      translation_dict, rotation_dict, color_mode=cv2.COLORMAP_JET)

    except KeyboardInterrupt:
        tq.close()
        writer.close()
        torch.cuda.empty_cache()

    tq.close()
    writer.close()
