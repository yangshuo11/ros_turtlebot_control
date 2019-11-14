#!/usr/bin/env python

# Common libraries
import argparse
import numpy as np
import sys
import math
import yaml
import time

# ROS
import rospy
import roslib
import rospy

# Geometric transformations
import tf
from tf.transformations import euler_from_quaternion

# Messages
from std_msgs.msg import Header
from geometry_msgs.msg import Point, Quaternion, Pose, Twist, Vector3
from gazebo_msgs.srv import SetModelState
from gazebo_msgs.msg import ModelState, ModelStates
from nav_msgs.msg import Odometry
from std_msgs.msg import Empty


''' -------------------------------------------------------------------------- '''


class Math:
    # Mathmatical operations

    @staticmethod
    def xytheta_to_T(x, y, theta):
        ''' Convert robot 2D pose (x, y, theta)
            to 3x3 transformation matrix '''
        c = np.cos(theta)
        s = np.sin(theta)
        T = np.array([
            [c, -s, x, ],
            [s,  c, y, ],
            [0,  0, 1, ],
        ])
        return T

    @staticmethod
    def T_to_xytheta(T):
        ''' Convert 3x3 transformation matrix
            to robot 2D pose (x, y, theta) '''
        assert T.shape == (3, 3)
        x = T[0, 2]
        y = T[1, 2]
        s, c = T[1, 0], T[0, 0]
        theta = np.arctan2(s, c)
        return x, y, theta

    @staticmethod
    def _euler_from_quaternion(quat_xyzw):
        ''' An overload of tf.transformations.euler_from_quaternion '''
        def convert_quaternion_data_to_list(quat_xyzw):
            if type(quat_xyzw) != list and type(quat_xyzw) != np.ndarray:
                quat_xyzw = [quat_xyzw.x, quat_xyzw.y,
                             quat_xyzw.z, quat_xyzw.w]
            return quat_xyzw
        quat_xyzw = convert_quaternion_data_to_list(quat_xyzw)
        euler_xyz = euler_from_quaternion(quat_xyzw, 'rxyz')
        return euler_xyz

    @staticmethod
    def pose_to_xytheta(pose):
        x = pose.position.x
        y = pose.position.y
        euler_xyz = Math._euler_from_quaternion(pose.orientation)
        theta = euler_xyz[2]
        return x, y, theta

    @staticmethod
    def calc_dist(x1, y1, x2, y2):
        return ((x1 - x2)**2 + (y1 - y2)**2)**(0.5)

    @staticmethod
    def pi2pi(theta):
        return (theta + math.pi) % (2 * math.pi) - math.pi


''' -------------------------------------------------------------------------- '''


class SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __repr__(self):
        keys = sorted(self.__dict__)
        items = ("{}={!r}".format(k, self.__dict__[k]) for k in keys)
        return "{}({})".format(type(self).__name__, ", ".join(items))

    def __eq__(self, other):
        return self.__dict__ == other.__dict__


def dict2class(d):
    ''' Convert a dict to a class '''
    args = SimpleNamespace()
    args.__dict__.update(**d)
    return args


def ReadYamlFile(filepath):
    ''' Read contents from the yaml file.
    Output:
        data_loaded {dict}: contents of the yaml file.
    '''
    with open(filepath, 'r') as stream:
        data_loaded = yaml.safe_load(stream)
    return data_loaded


''' -------------------------------------------------------------------------- '''


def call_ros_service(service_name, service_type, service_cfg=None):
    ''' Call a ROS service
    '''
    rospy.wait_for_service(service_name)
    try:
        func = rospy.ServiceProxy(service_name, service_type)
        func(*service_cfg) if service_cfg else func()
    except rospy.ServiceException as e:
        print("Failed to call service:", service_name)
        sys.exit()


''' -------------------------------------------------------------------------- '''


class PidController(object):
    ''' PID controller '''

    def __init__(self, T, P=0, I=0, D=0):
        ''' Arguments
        T {float}: Control period. Unit: second.
            This is the inverse of control frequency.
        P {float or np.array}: Proportional control coefficient.
        I {float or np.array}: Integral control coefficient.
        D {float or np.array}: Differential control coefficient.
        '''

        # -- Check input data
        b1 = all(isinstance(d, float) for d in [P, I, D])
        b2 = all(isinstance(d, np.ndarray) for d in [P, I, D])
        if not b1 and not b2:
            err_msg = "PidController: Data type of P,I,D coefficient is wrong."
            raise RuntimeError(err_msg)
        dim = 1 if b1 else len(P)  # Dimension of the control variable

        # -- Initialize arguments.
        self._T = T
        self._P = np.zeros(dim)+P
        self._I = np.zeros(dim)+I
        self._D = np.zeros(dim)+D
        self._err_inte = np.zeros(dim)  # Integration error.
        self._err_prev = np.zeros(dim)  # Previous error.

    def compute(self, err):
        ''' Given the error, compute the desired control value . '''
        ctrl_val = 0
        err = np.array(err)

        # P
        ctrl_val += np.dot(err, self._P)

        # I
        self._err_inte += err
        ctrl_val += self._T * np.dot(self._err_inte, self._I)

        # D
        ctrl_val += np.dot(err-self._err_prev, self._D) / self._T

        self._err_prev = err
        return ctrl_val


''' -------------------------------------------------------------------------- '''


class Turtle(object):
    ''' A `turtle` class for controlling turtlebot3. '''

    def __init__(self, config_filepath="config/config.yaml"):
        '''
        Arguments:
            turtle_name {str}: This is for "/gazebo/set_model_state"
            ref_frame {str}
            is_in_simulation {bool}:
                Is in simulation, or is real robot.
            topic_set_turlte_speed {str}:
                Topic for setting turtlebot speed.
            topic_get_turtle_speed_env_sim {str}:
                Topic to get turtlebot speed if it's in simulation
            topic_get_turtle_speed_env_real {str}:
                Topic to get turtlebot speed if it's a real turtlebot robot.
        '''
        self._cfg = dict2class(ReadYamlFile(config_filepath))

        # Publisher.
        self._pub_speed = rospy.Publisher(
            self._cfg.topic_set_turlte_speed, Twist, queue_size=10)

        # Subscriber.
        if self._cfg.is_in_simulation:
            self._sub_pose = rospy.Subscriber(
                self._cfg.topic_get_turtle_speed_env_sim,
                ModelStates, self._callback_sub_pose_env_sim)
        else:
            self._sub_pose = rospy.Subscriber(
                self._cfg.topic_get_turtle_speed_env_real,
                Odometry, self._callback_sub_pose_env_real)

        # Robot state.
        self._time0 = self._reset_time()
        self._pose = Pose()
        self._twist = Twist()

    def reset_pose(self):
        ''' Reset Robot pose.
        If in simulation, reset the simulated robot pose to (0, 0, 0).
        If real robot, reset the odometry and IMU data to zero.
        '''
        rospy.loginfo("Resetting robot state...")
        if self._is_in_simulation:
            self._reset_pose_env_sim()
        else:
            self._reset_pose_env_real()
        rospy.loginfo("Reset robot state completes")

    def set_robot_speed(self, v, w):
        ''' Set robot speed.
        Arguments:
            v {float}: linear speed
            w {float}: angular speed
        '''
        twist = Twist()
        twist.linear.x = v
        if self._cfg._is_in_simulation:
            twist.angular.z = -w
        else:
            twist.angular.z = w
        self._pub_speed.publish(twist)

    def get_robot_pose(self):
        x, y, theta = Math.pose_to_xytheta(self._pose)
        return x, y, theta

    def reset_time(self):
        self._time0 = rospy.get_time()
        return self._time0

    def query_time(self):
        return rospy.get_time() - self._time0


    def print_state(self, x, y, theta, v=np.nan, w=np.nan):
        print("Robot pose: "
              "x = {:.3f}, "
              "y = {:.3f}, "
              "theta = {:.3f}, "
              "v = {:.3f}, "
              "w = {:.3f}".format(
                  x, y, theta, v, w))

    def move_a_circle(self, v=0.1, w=0.1):
        ''' Control the turtlebot to move in a circle
            until the program stops.
        '''
        while not rospy.is_shutdown():
            self._set_twist(v, w)

            # Print state
            x, y, theta = self._get_pose()
            self._print_state(x, y, theta, v, w)
            print("Moving in circle ...")
            rospy.sleep(0.5)
        return True

    def move_forward(self, v=0.1):
        ''' Control the turtlebot to move forward
            until the program stops.
        '''
        w = 0
        while not rospy.is_shutdown():
            self._set_twist(v, w)

            # Print state
            x, y, theta = self._get_pose()
            self._print_state(x, y, theta, v, w)
            print("Moving forward ...")
            rospy.sleep(0.5)
        return True

    def move_to_pose(self, x_goal_w, y_goal_w, theta_goal_w=None):
        ''' 
        Control the turtlebot to move towards the target pose (Absolute pose).
            This function returns after the robot gets very close to the target.
        Arguments:
            x_goal_w, y_goal_w, theta_goal_w {float}: 
                The target pose represented in the world frame.
        '''

        print("\nMove robot to the global pose: {}, {}, {}\n".format(
            x_goal_w, y_goal_w, theta_goal_w))

        self._move_robot_to_pose(
            x_goal_w, y_goal_w, theta_goal_w,
            self._cfg.x_tol, self._cfg.y_tol, self._cfg.theta_tol)

        return True

    def move_to_relative_pose(self, x_goal_r, y_goal_r, theta_goal_r):
        ''' 
        Control the turtlebot to move towards the target pose (Relative pose).
            This function returns after the robot gets very close to the target
        Arguments:
            x_goal_r, y_goal_r, theta_goal_r {float}:
                The target pose represented by the robot frame,
                where x is front, y is left, and theta is from x to y.
        '''
        # Convert target pose from robot frame to world frame.
        x_goal_w, y_goal_w, theta_goal_w = self._pose_robot2world(
            x_goal_r, y_goal_r, theta_goal_r)

        # Move.
        print("\nMove robot to the global pose: {}, {}, {}\n".format(
            x_goal_w, y_goal_w, theta_goal_w))
        self._move_to_pose(
            x_goal_w, y_goal_w, theta_goal_w,
            self._cfg.x_tol, self._cfg.y_tol, self._cfg.theta_tol)

        return True

    def _pose_robot2world(self, x_rg, y_rg, theta_rg):
        ''' 
        Transform the coordinate:
            from:   robot_frame's goal pose (X_rg)
            to:     world_frame's goal pose (X_wg)
        where X is the robot state (x, y, theta).
        '''
        x_wr, y_wr, theta_wr = self._get_pose()
        T_wr = Math.xytheta_to_T(x_wr, y_wr, theta_wr)  # T_world_to_robot
        T_rg = Math.xytheta_to_T(x_rg, y_rg, theta_rg)  # T_robot_to_goal
        T_wg = np.dot(T_wr, T_rg)
        x_wg, y_wg, theta_wg = Math.T_to_xytheta(T_wg)
        return x_wg, y_wg, theta_wg

    def _move_robot_to_pose(
            self, x_goal, y_goal, theta_goal=None,
            x_tol=0.01, y_tol=0.01, theta_tol=0.1):
        ''' 
        Control the turlebot to the target pose.
        This function returns after the robot gets very close to the target

        If theta_goal is None, 
            use `drive to point` algorithm, where k_beta=0.
        If theta_goal is not None, 
            use `drive to pose` algorithm. 

        Reference: page 129 of the book "Robotics, Vision, and Control".
        '''

        # Robot config
        MAX_V = 0.2
        MAX_W = 0.6
        MIN_V = 0.0  # should be 0
        MIN_W = 0.0  # should be 0

        # Set control parameters
        T = 0.05  # control period
        PidController.set_control_period(T)
        exp_ratio = 1.0  # exp ratio applied to PID's output. should be 1
        k_vals = [0.5, 1.0, -0.5]
        # k_vals = [0.5, 1.2, -0.6]

        k_rho = k_vals[0]  # reduce distance to the goal. P > 0
        k_alpha = k_vals[1]  # drive robot towards the goal. P > P_rho
        if theta_goal is None:
            theta_goal = 0
            k_beta = 0  # not considering orientation
        else:
            k_beta = k_vals[2]  # make robot same orientation as desired. P < 0
            # 100% is too large

        # Init PID controllers
        pid_rho = PidController(P=k_rho, I=0)
        pid_alpha = PidController(P=k_alpha, I=0)
        pid_beta = PidController(P=k_beta, I=0)

        # Loop and control
        cnt_steps = 0
        while not rospy.is_shutdown():
            cnt_steps += 1

            x, y, theta = self.get_robot_pose()

            rho = Math.calc_dist(x, y, x_goal, y_goal)
            alpha = Math.pi2pi(math.atan2(y_goal - y, x_goal - x) - theta)
            beta = - theta - alpha + theta_goal

            # check direction
            sign = 1
            if abs(alpha) > math.pi/2:  # the goal is behind the robot
                alpha = Math.pi2pi(math.pi - alpha)
                beta = Math.pi2pi(math.pi - beta)
                sign = -1

            # Pass error into PID controller and obtain control output
            val_rho = pid_rho.compute(err=rho)[0]
            val_alpha = pid_alpha.compute(err=alpha)[0]
            val_beta = pid_beta.compute(err=beta)[0]

            # Get v and w
            v = sign * val_rho**exp_ratio
            w = sign * (val_alpha + val_beta)**exp_ratio

            # Threshold on velocity
            v = max(MIN_V, min(abs(v), MAX_V)) * \
                (1 if v > 0 else -1)  # limit v
            w = max(MIN_W, min(abs(w), MAX_W)) * \
                (1 if w > 0 else -1)  # limit w

            # Output
            self.set_robot_speed(v, w)
            if cnt_steps % 10 == 0:
                self.print_state(x, y, theta, v, w)
                print("\trho = {}, alpha = {}, beta = {}".format(rho, alpha, beta))

            rospy.sleep(T)

            # Check stop condition
            if abs(x - x_goal) < x_tol and \
                    abs(y-y_goal) < y_tol and \
                    abs(theta-theta_goal) < theta_tol:
                break

        self.set_robot_speed(v=0, w=0)
        print("Reach the target. Control completes.\n")

    def _reset_pose_env_real(self):
        ''' Reset the robot pose (For real robot mode).
        This is the same as the terminal command:
            $ rostopic pub /reset std_msgs/Empty  '{}'
        '''
        if self._clf.is_in_simulation:
            raise RuntimeError(
                "In `simulation` mode, this function shouldn't be called.")
        reset_odom = rospy.Publisher(
            self._cfg.topic_reset_pose_env_real,
            Empty, queue_size=10)
        reset_odom.publish(Empty())

    def _reset_pose_env_sim(self):
        ''' Reset the robot pose (For simulation mode).
        This is the same as the terminal command:
            $ rostopic pub /gazebo/set_model_state gazebo_msgs/ModelState \
            '{  turtle_name: turtlebot3_waffle_pi, \
                pose: {     position: { x: 0, y: 0, z: 0 }, \
                            orientation: {x: 0, y: 0, z: 0, w: 1 } }, \
                twist: {    linear: { x: 0, y: 0, z: 0 }, \
                            angular: { x: 0, y: 0, z: 0}  }, \
                ref_frame: world }'
        '''
        if not self._clf.is_in_simulation:
            raise RuntimeError(
                "In `real robot` mode, this function shouldn't be called.")

        # Set robot `Zero` state
        x, y, z = 0, 0, 0
        p = Point(x=x, y=y, z=z)
        q = Quaternion(x=0, y=0, z=0, w=0)
        state = ModelState(
            pose=Pose(position=p, orientation=q),
            twist=Twist(),
            turtle_name=self._cfg.turtle_name,
            ref_frame=self._cfg.ref_frame)

        # Call service to set position
        call_ros_service(
            service_name=self._cfg.srv_reset_pose_env_sim,
            service_type=SetModelState,
            args=(state, )
        )


    def _callback_sub_pose_env_sim(self, model_states):
        ''' ROS topic subscriber's callback function
            for receiving and updating robot pose when running simulation.
        '''
        idx = model_states.name.index(self._cfg.turtle_name)
        self._pose = model_states.pose[idx]
        self._twist = model_states.twist[idx]

    def _callback_sub_pose_env_real(self, odometry):
        ''' ROS topic subscriber's callback function
            for receiving and updating robot pose when running robot.
        '''
        # Contents of odometry:
        #   frame_id: "odom"
        #   child_frame_id: "base_footprint"
        self._pose = odometry.pose.pose
        self._twist = odometry.twist.twist