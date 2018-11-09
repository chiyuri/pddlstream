from __future__ import print_function

import argparse
import cProfile
import pstats
import random
import time

import numpy as np
from pydrake.geometry import (ConnectDrakeVisualizer, DispatchLoadMessage)
from pydrake.lcm import DrakeLcm  # Required else "ConnectDrakeVisualizer(): incompatible function arguments."
from pydrake.systems.analysis import Simulator
from pydrake.systems.framework import DiagramBuilder
from pydrake.systems.primitives import SignalLogger

from examples.drake.generators import Pose, Conf, get_pose_gen, get_grasp_gen, get_ik_fn, \
    get_motion_fn, get_pull_fn, get_collision_test, get_reachable_pose_gen
from examples.drake.iiwa_utils import open_wsg50_gripper, get_door_open_positions
from examples.drake.postprocessing import postprocess_plan, compute_duration, convert_splines
from examples.drake.problems import load_manipulation, load_tables, load_station
from examples.drake.utils import get_model_joints, get_world_pose, set_world_pose, set_joint_position, \
    prune_fixed_joints, get_configuration, get_model_name, user_input, get_joint_positions, get_parent_joints, \
    get_state, set_state, RenderSystemWithGraphviz

from pddlstream.algorithms.focused import solve_focused
from pddlstream.language.constants import And
from pddlstream.language.generator import from_gen_fn, from_fn
from pddlstream.language.function import FunctionInfo
from pddlstream.utils import print_solution, read, INF, get_file_path


# Listing all available docker images
# https://stackoverflow.com/questions/28320134/how-to-list-all-tags-for-a-docker-image-on-a-remote-registry
# wget -q https://registry.hub.docker.com/v1/repositories/mit6881/drake-course/tags -O -  | sed -e 's/[][]//g' -e 's/"//g' -e 's/ //g' | tr '}' '\n'  | awk -F: '{print $3}'

# Removing old docker images
# docker rmi $(docker images -q mit6881/drake-course)

# Converting from URDF to SDF
# gz sdf -p ../urdf/iiwa14_polytope_collision.urdf > /iiwa14_polytope_collision.sdf

##################################################


def add_meshcat_visualizer(scene_graph, builder):
    # https://github.com/rdeits/meshcat-python
    # https://github.com/RussTedrake/underactuated/blob/master/src/underactuated/meshcat_visualizer.py
    from underactuated.meshcat_visualizer import MeshcatVisualizer
    viz = MeshcatVisualizer(scene_graph, draw_timestep=0.033333)
    builder.AddSystem(viz)
    builder.Connect(scene_graph.get_pose_bundle_output_port(),
                    viz.get_input_port(0))
    viz.load()
    return viz


def add_drake_visualizer(scene_graph, builder):
    lcm = DrakeLcm()
    ConnectDrakeVisualizer(builder=builder, scene_graph=scene_graph, lcm=lcm)
    DispatchLoadMessage(scene_graph, lcm) # TODO: only update viewer after a plan is found
    return lcm # Important that variable is saved


def connect_controllers(builder, mbp, robot, gripper, print_period=1.0):
    from examples.drake.kuka_multibody_controllers import (KukaMultibodyController, HandController, ManipStateMachine)

    iiwa_controller = KukaMultibodyController(plant=mbp,
                                              kuka_model_instance=robot,
                                              print_period=print_period)
    builder.AddSystem(iiwa_controller)

    builder.Connect(iiwa_controller.get_output_port(0),
                    mbp.GetInputPort('iiwa_actuation'))
    builder.Connect(mbp.get_continuous_state_output_port(),
                    iiwa_controller.robot_state_input_port)

    hand_controller = HandController(plant=mbp,
                                     model_instance=gripper)
    builder.AddSystem(hand_controller)
    builder.Connect(hand_controller.get_output_port(0),
                    mbp.GetInputPort('gripper_actuation'))
    builder.Connect(mbp.get_continuous_state_output_port(),
                    hand_controller.robot_state_input_port)

    state_machine = ManipStateMachine(mbp)
    builder.AddSystem(state_machine)
    builder.Connect(mbp.get_continuous_state_output_port(),
                    state_machine.robot_state_input_port)
    builder.Connect(state_machine.kuka_plan_output_port,
                    iiwa_controller.plan_input_port)
    builder.Connect(state_machine.hand_setpoint_output_port,
                    hand_controller.setpoint_input_port)
    return state_machine


def build_diagram(mbp, scene_graph, meshcat=False):
    builder = DiagramBuilder()
    builder.AddSystem(scene_graph)
    builder.AddSystem(mbp)

    # Connect scene_graph to MBP for collision detection.
    builder.Connect(
        mbp.get_geometry_poses_output_port(),
        scene_graph.get_source_pose_port(mbp.get_source_id()))
    builder.Connect(
        scene_graph.get_query_output_port(),
        mbp.get_geometry_query_input_port())

    if meshcat:
        vis = add_meshcat_visualizer(scene_graph, builder)
    else:
        vis = add_drake_visualizer(scene_graph, builder)

    state_log = builder.AddSystem(SignalLogger(mbp.get_continuous_state_output_port().size()))
    state_log._DeclarePeriodicPublish(0.02)
    builder.Connect(mbp.get_continuous_state_output_port(), state_log.get_input_port(0))

    #return builder.Build()
    return builder

##################################################

def get_pddlstream_problem(task, context, collisions=True):
    domain_pddl = read(get_file_path(__file__, 'domain.pddl'))
    stream_pddl = read(get_file_path(__file__, 'stream.pddl'))
    constant_map = {}

    mbp = task.mbp
    robot = task.robot
    robot_name = get_model_name(mbp, robot)

    world = mbp.world_frame() # mbp.world_body()
    robot_joints = prune_fixed_joints(get_model_joints(mbp, robot))
    robot_conf = Conf(robot_joints, get_configuration(mbp, context, robot))
    init = [
        ('Robot', robot_name),
        ('CanMove', robot_name),
        ('Conf', robot_name, robot_conf),
        ('AtConf', robot_name, robot_conf),
        ('HandEmpty', robot_name),
    ]
    goal_literals = []
    if task.reset_robot:
        goal_literals.append(('AtConf', robot_name, robot_conf),)

    for obj in task.movable:
        obj_name = get_model_name(mbp, obj)
        #obj_frame = get_base_body(mbp, obj).body_frame()
        obj_pose = Pose(mbp, world, obj, get_world_pose(mbp, context, obj)) # get_relative_transform
        init += [('Graspable', obj_name),
                 ('Pose', obj_name, obj_pose),
                 #('InitPose', obj_name, obj_pose),
                 ('AtPose', obj_name, obj_pose)]
        for surface in task.surfaces:
            init += [('Stackable', obj_name, surface)]
            #if is_placement(body, surface):
            #    init += [('Supported', body, pose, surface)]

    for surface in task.surfaces:
        surface_name = get_model_name(mbp, surface.model_index)
        if 'sink' in surface_name:
            init += [('Sink', surface)]
        if 'stove' in surface_name:
            init += [('Stove', surface)]

    for door in task.doors:
        door_body = mbp.tree().get_body(door)
        door_name = door_body.name()
        door_joints = get_parent_joints(mbp, door_body)
        door_conf = Conf(door_joints, get_joint_positions(door_joints, context))
        init += [
            ('Door', door_name),
            ('Conf', door_name, door_conf),
            ('AtConf', door_name, door_conf),
        ]
        for positions in [get_door_open_positions(door_body)]: #, get_closed_positions(door_body)]:
            conf = Conf(door_joints, positions)
            init += [('Conf', door_name, conf)]
            #goal_literals += [('AtConf', door_name, conf)]
        if task.reset_doors:
            goal_literals += [('AtConf', door_name, door_conf)]

    for obj, transform in task.goal_poses.items():
        obj_name = get_model_name(mbp, obj)
        obj_pose = Pose(mbp, world, obj, transform)
        init += [('Pose', obj_name, obj_pose)]
        goal_literals.append(('AtPose', obj_name, obj_pose))
    for obj in task.goal_holding:
        goal_literals.append(('Holding', robot_name, get_model_name(mbp, obj)))
    for obj, surface in task.goal_on:
        goal_literals.append(('On', get_model_name(mbp, obj), surface))
    for obj in task.goal_cooked:
        goal_literals.append(('Cooked', get_model_name(mbp, obj)))

    goal = And(*goal_literals)
    print('Initial:', init)
    print('Goal:', goal)

    stream_map = {
        #'sample-pose': from_gen_fn(get_stable_gen(task, context, collisions=collisions)),
        'sample-reachable-pose': from_gen_fn(get_reachable_pose_gen(task, context, collisions=collisions)),
        'sample-grasp': from_gen_fn(get_grasp_gen(task)),
        'plan-ik': from_fn(get_ik_fn(task, context, collisions=collisions)),
        'plan-motion': from_fn(get_motion_fn(task, context, collisions=collisions)),
        'plan-pull': from_fn(get_pull_fn(task, context, collisions=collisions)),
        'TrajPoseCollision': get_collision_test(task, context, collisions=collisions),
        'TrajConfCollision': get_collision_test(task, context, collisions=collisions),
    }
    #stream_map = 'debug'

    return domain_pddl, constant_map, stream_pddl, stream_map, init, goal

def plan_trajectories(task, context, collisions=True):
    stream_info = {
        'TrajPoseCollision': FunctionInfo(p_success=1e-3, eager=False),
        'TrajConfCollision': FunctionInfo(p_success=1e-3, eager=False),
    }
    problem = get_pddlstream_problem(task, context, collisions=collisions)
    pr = cProfile.Profile()
    pr.enable()
    solution = solve_focused(problem, stream_info=stream_info, planner='ff-wastar2',
                             max_cost=INF, max_time=180, debug=False,
                             unit_efforts=True, effort_weight=1, search_sampling_ratio=0)
    pr.disable()
    pstats.Stats(pr).sort_stats('tottime').print_stats(10)
    print_solution(solution)
    plan, cost, evaluations = solution
    if plan is None:
        return None
    return postprocess_plan(task.mbp, task.gripper, plan)

##################################################

def step_trajectories(diagram, diagram_context, context, trajectories, time_step=0.001, teleport=False):
    diagram.Publish(diagram_context)
    user_input('Step?')
    for traj in trajectories:
        if teleport:
            traj.path = traj.path[::len(traj.path)-1]
        for _ in traj.iterate(context):
            diagram.Publish(diagram_context)
            if time_step is None:
                user_input('Continue?')
            else:
                time.sleep(time_step)
    user_input('Finish?')

def simulate_splines(diagram, diagram_context, sim_duration, real_time_rate=1.0):
    simulator = Simulator(diagram, diagram_context)
    simulator.set_publish_every_time_step(False)
    simulator.set_target_realtime_rate(real_time_rate)
    simulator.Initialize()

    diagram.Publish(diagram_context)
    user_input('Simulate?')
    simulator.StepTo(sim_duration)
    user_input('Finish?')


##################################################

def test_manipulation(plan_list, gripper_setpoint_list, is_hardware=False):
    from pydrake.common import FindResourceOrThrow
    from .lab_1.manipulation_station_simulator import ManipulationStationSimulator

    object_file_path = FindResourceOrThrow(
            "drake/examples/manipulation_station/models/061_foam_brick.sdf")

    manip_station_sim = ManipulationStationSimulator(
        time_step=1e-3,
        object_file_path=object_file_path,
        object_base_link_name="base_link",
        is_hardware=is_hardware)

    if is_hardware:
        iiwa_position_command_log = manip_station_sim.RunRealRobot(plan_list, gripper_setpoint_list)
    else:
        q0 = [0, 0.6 - np.pi / 6, 0, -1.75, 0, 1.0, 0]
        #q0[1] += np.pi/6
        iiwa_position_command_log = manip_station_sim.RunSimulation(plan_list, gripper_setpoint_list,
                                                                    extra_time=2.0, q0_kuka=q0)
    return iiwa_position_command_log

##################################################

PROBLEMS = [
    load_tables,
    load_manipulation,
    load_station,
]

def main(deterministic=False):
    # TODO: GeometryInstance, InternalGeometry, & GeometryContext to get the shape of objects
    # TODO: cost-sensitive planning to avoid large kuka moves
    # TODO: get_contact_results_output_port
    # TODO: gripper closing via collision information

    time_step = 0.0002 # TODO: context.get_continuous_state_vector() fails
    if deterministic:
        # TODO: still not fully deterministic
        random.seed(0)
        np.random.seed(0)

    parser = argparse.ArgumentParser()
    parser.add_argument('-p', '--problem', default='load_manipulation', help='The name of the problem to solve')
    parser.add_argument('-c', '--cfree', action='store_true', help='Disables collisions when planning')
    parser.add_argument('-v', '--visualizer', action='store_true', help='Use the drake visualizer')
    parser.add_argument('-s', '--simulate', action='store_true', help='Simulates the system')
    args = parser.parse_args()

    problem_fn_from_name = {fn.__name__: fn for fn in PROBLEMS}
    if args.problem not in problem_fn_from_name:
        raise ValueError(args.problem)
    print('Problem:', args.problem)
    problem_fn = problem_fn_from_name[args.problem]

    meshcat_vis = None
    if not args.visualizer:
        import meshcat
        # Important that variable is saved
        meshcat_vis = meshcat.Visualizer()  # vis.set_object
        # http://127.0.0.1:7000/static/

    mbp, scene_graph, task = problem_fn(time_step=time_step)
    #station, mbp, scene_graph = load_station(time_step=time_step)
    #builder.AddSystem(station)
    #dump_plant(mbp)
    #dump_models(mbp)
    print(task)
    #print(sorted(body.name() for body in task.movable_bodies()))
    #print(sorted(body.name() for body in task.fixed_bodies()))

    ##################################################

    builder = build_diagram(mbp, scene_graph, not args.visualizer)
    if args.simulate:
        state_machine = connect_controllers(builder, mbp, task.robot, task.gripper)
    else:
        state_machine = None
    diagram = builder.Build()
    RenderSystemWithGraphviz(diagram) # Useful for getting port names
    diagram_context = diagram.CreateDefaultContext()
    context = diagram.GetMutableSubsystemContext(mbp, diagram_context)
    task.diagram = diagram
    task.diagram_context = diagram_context
    task.meshcat_vis = meshcat_vis

    #context = mbp.CreateDefaultContext()
    for joint, position in task.initial_positions.items():
        set_joint_position(joint, context, position)
    for model, pose in task.initial_poses.items():
        set_world_pose(mbp, context, model, pose)
    open_wsg50_gripper(mbp, context, task.gripper)

    diagram.Publish(diagram_context)
    initial_state = get_state(mbp, context)
    trajectories = plan_trajectories(task, context, not args.cfree)
    if trajectories is None:
        return

    ##################################################

    set_state(mbp, context, initial_state)
    if args.simulate:
        splines, gripper_setpoints = convert_splines(mbp, task.robot, task.gripper, context, trajectories)
        sim_duration = compute_duration(splines)
        print('Splines: {}\nDuration: {:.3f} seconds'.format(len(splines), sim_duration))
        set_state(mbp, context, initial_state)

        if True:
            state_machine.Load(splines, gripper_setpoints)
            simulate_splines(diagram, diagram_context, sim_duration)
        else:
            # NOTE: there is a plan that moves home initially for 15 seconds
            from .lab_1.robot_plans import JointSpacePlan
            plan_list = [JointSpacePlan(spline) for spline in splines]
            #meshcat_vis.delete()
            user_input('Simulate?')
            test_manipulation(plan_list, gripper_setpoints)
    else:
        #time_step = None
        time_step = 0.001 if meshcat else 0.02
        step_trajectories(diagram, diagram_context, context, trajectories, time_step=time_step) #, teleport=True)


if __name__ == '__main__':
    main()

# .../pddlstream$ python2 -m examples.drake.run
