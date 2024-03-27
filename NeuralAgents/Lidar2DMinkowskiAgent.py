import scipy.misc
import numpy as np
import cv2
import os
import sys
#sys.path.append('.')
#sys.path.append('../')
import glob
import math
import yaml
import carla
import argparse
from collections import deque

from agents.navigation.basic_agent import *

from AutoCastSim.srunner.scenariomanager.carla_data_provider import CarlaDataProvider

from AutoCastSim.srunner.autoagents.autonomous_agent import AutonomousAgent

from AutoCastSim.srunner.autoagents.sensor_interface import SensorInterface
from AutoCastSim.srunner.scenariomanager.carla_data_provider import CarlaDataProvider, CarlaActorPool
from agents.navigation.basic_agent import BasicAgent
from AutoCastSim.AVR.autocast_agents.new_agent import NewAgent


from AutoCastSim.AVR import Utils
from AutoCastSim.AVR.PCProcess import LidarPreprocessor
#transform_pointcloud, convert_json_to_transform, transform_coords
from AutoCastSim.AVR.DataLogger import DataLogger
from AutoCastSim.AVR import Collaborator
import torch
from torchvision import transforms
import torch.nn.functional as F
from AutoCastSim.AVR import Utils
from NeuralAgents.controller import ls_circle, project_point_to_circle, signed_angle
#from .controller import PIDController, CustomController
from models import SparsePolicyNet, SparseControlNet, SparseSpeedControlNet

from MinkowskiEngine.utils import sparse_quantize, sparse_collate, batched_coordinates
import MinkowskiEngine as ME

class Lidar2DMinkowskiAgent(AutonomousAgent):
    
    def __init__(self, path_to_conf_file):
        super().__init__(path_to_conf_file)
        config = yaml.load(open(path_to_conf_file))
        self.T = config['T']['value']
        self.num_commands = config['num_commands']['value']
        self.ego_only = config['ego_only']['value']
        self.shared = config['shared']['value']
        self.num_hidden = config['num_hidden']['value']
        self.model_checkpoint = 'model-200.th'
        
        self.model_path = os.path.join(os.path.split(path_to_conf_file)[0], self.model_checkpoint)
        self.num_output = 3
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu') 
        
        self.use_speed = config["use_speed"]["value"]
        self.frame_stack = config["frame_stack"]["value"]

        config = argparse.ArgumentParser()
        config.T = self.T
        config.num_hidden = self.num_hidden
        config.num_commands = self.num_commands
        config.use_speed = self.use_speed
        config.frame_stack = self.frame_stack
        self.learn_control = True
        self.coord_buff = deque()
        self.feat_buff = deque()
        print("Stacking ", self.frame_stack, " frames")
        print("Use Speed ?", self.use_speed)
        if self.learn_control:
            if self.use_speed:
                model = SparseSpeedControlNet(config).to(self.device)
            else:
                model = SparseControlNet(config).to(self.device)
        else:
            model = SparsePolicyNet(config).to(self.device)
        model.eval()
        print("Loading model and initializing ", self.model_path)
        model.load_state_dict(torch.load(self.model_path))
        self.model = model
        self.count = 0
        self.agent_trajectory_points_timestamp = []
        self.collider_trajectory_points_timestamp = []
        self.next_target_location = None
        self.drawing_object_list = []
        self.transform = transforms.ToTensor()
        
        self._agent_contrl = None
        self._target_radius = 2.0
        self._final_goal = None
        self._agent = None
        self._route_assigned = False
        self._target_speed = 20  # default 20 km/h
        
        # Controllers
        # self.speed_control = PIDController(K_P=1.0, K_I=0.1, K_D=2.5)

        #if steer_points is None:
        #    steer_points = {"1": 3, "2": 2, "3": 2, "4": 2}
        
        #if pid is None:
        #    pid = {
        #        "1" : {"Kp": 1.0, "Ki": 0.1, "Kd":0}, # Left
        #        "2" : {"Kp": 1.0, "Ki": 0.1, "Kd":0}, # Right
        #        "3" : {"Kp": 0.8, "Ki": 0.1, "Kd":0}, # Straight
        #        "4" : {"Kp": 0.8, "Ki": 0.1, "Kd":0}, # Follow
        #    }
            
        #self.turn_control = CustomController(pid)

    def setup(self, path_to_conf_file):
        self.lidar_dim = LidarPreprocessor.lidar_dim
        self.lidar_depth_dim = LidarPreprocessor.lidar_depth_dim
        self.count = 0

    def sensors(self):
        """
        Define the sensor suite required by the agent
        :return: a list containing the required sensors in the following format:
        """
        sensors = [
            {'type': 'sensor.camera.rgb', 
             'x': Utils.LidarRoofForwardDistance, 'y': 0.0, 'z': Utils.LidarRange,
             'roll': 0.0, 'pitch': -90.0, 'yaw': 0.0,
             'width': 720, 'height': 720, 'fov': 90, 
             'id': 'RGB'},  
             # use same width height to align with lidar display
            
            {'type': 'sensor.lidar.ray_cast', 
             'x': Utils.LidarRoofForwardDistance, 'y': 0.0,'z': Utils.LidarRoofTopDistance,  
             # the spawn function will add this on top of bbox.extent.z
             'yaw': Utils.LidarYawCorrection, 'pitch': 0.0, 'roll': 0.0,
             'range': Utils.LidarRange,
             # set same as camera height, cuz camera fov is 90 deg, HUD can visualize in same dimension
             'rotation_frequency': 20, 
             'channels': 64,
             'upper_fov': 4, 
             'lower_fov': -20, 
             'points_per_second': 2304000,
             'id': 'LIDAR'},
        ]

        return sensors

    def run_step(self, input_data, timestamp, JSONState=None):
        if not self._agent:
            hero_actor = CarlaActorPool.get_hero_actor()
            if hero_actor:
                self._agent = NewAgent(hero_actor, self._target_speed)

        # Use NPC agent result
        control = super().run_step(input_data, timestamp)
        # Obtain ego Lidar data and tranform it to be BEV
        lidar = input_data[str(self._agent.id) + Collaborator.LidarSensorName][1]
        # Obtain Fused Lidar data
        fused_points = input_data[str(self._agent.id)+Collaborator.FusedLidarSensorName][1]
        if fused_points.id != -1 and self.shared:
            print("Using fused sensor")
            fused_lidar = fused_points.pc 
            lidar = fused_lidar
        coord, feat = np.asarray(lidar[:,:2]), np.asarray(lidar[:,2,None])
        coord, feat = sparse_quantize(coord, feat, quantization_size=(LidarPreprocessor.dX, LidarPreprocessor.dY))
        #new_coord = [np.append(d,-1) if len(d)==2 for d in coord]
        new_coord = []
        for i in range(len(coord)):
            d=coord[i]
            if len(d)<2:
                if len(d)==1:
                    d=d[0]
                if len(d)==1:
                    d=d[0]
                if len(d)<=1 or d is None:
                    print("$$$$$$$$$$$$$$$$$$$$$$$",d,feat[i])
                    from IPython import embed; embed()
                    continue
            new_coord.append(np.append(d,-1))
        coord = np.array(new_coord)
        if len(self.coord_buff) >= self.frame_stack: 
            self.coord_buff.popleft()
            self.feat_buff.popleft()
        self.coord_buff.append(new_coord)
        self.feat_buff.append(feat)
        
        coord, feat = [],[]
        for i in range(len(self.coord_buff)):
            c_buff = self.coord_buff[i]
            f_buff = self.feat_buff[i]
            self.coord_buff[i] = [ c + np.array([0,0,1]) for c in c_buff]
            coord.extend(self.coord_buff[i])
            feat.extend(f_buff)
        coord, feat = np.array(coord), np.array(feat) 
        
        coord_batch, feat_batch = sparse_collate([coord], [feat])
        fused_sensor_id = str(self._agent.id) + Collaborator.FusedLidarSensorName
        frame_id = input_data[fused_sensor_id][0]
        JSONState = DataLogger.compile_actor_state(self, frame_id)[0]
        pred_location, pred_brake, speed, pred_control = None, None, None, None
        learn_control = self.learn_control
        classification = True
        if JSONState is not None:
            ego_id = self._agent.id
            ego_actor_info = JSONState['other_actors'][ego_id]
            current_transform = ego_actor_info['transform']
            egoTrans = Utils.convert_json_to_transform(JSONState['ego_vehicle_transform'])
            ego_speed = torch.tensor(np.array([ego_actor_info['velocity']])).float().to(self.device)
            speed = ego_actor_info['velocity']
            ego_command = torch.tensor(np.array([3])).to(self.device)

            bev = ME.SparseTensor(feat_batch.float(), coord_batch).to(self.device)
            if not self.learn_control:
                pred_location, pred_brake = self.model(bev, ego_speed, ego_command)
                pred_location = pred_location.cpu().detach().numpy()[0] 
                if classification:
                    pred_brake = F.sigmoid(pred_brake)
                    pred_brake = pred_brake.cpu().detach().numpy()[0]
                targets = pred_location[:,:2]
                targets = np.concatenate([[[0,0]], targets],0)
                c,r = ls_circle(targets)
                n = 1
                closest = project_point_to_circle(targets[n],c,r)
                
                v = [1.0, 0.0, 0.0]
                w = [closest[0], closest[1], 0.0]
                alpha = signed_angle(v, w)

            if self.learn_control:
                pred_throttle, pred_brake, pred_steer = self.model(bev,ego_speed,ego_command)
                pred_throttle = pred_throttle.cpu().detach().numpy()[0]
                pred_brake = pred_brake.cpu().detach().numpy()[0]
                pred_steer = pred_steer.cpu().detach().numpy()[0]

            #print('Target Speed', self._target_speed)
            #print('pre_transform_pred_location', pred_location)
            #print('ego_transform', egoTrans)
            #pred_location = np.array([[5,0,0]])
            #pred_location = Utils.robot_to_map_transform(pred_location, egoTrans)[0]
        #print('pred_location', pred_location)run-20210210_004416-aqz93m5n
        #print('predicted steer', alpha)
        
        if not self.learn_control:
            # PID control
            print("Predicted Brake:", pred_brake, "Speed:",speed)
            #control = carla.VehicleControl()
            control = self._agent._local_planner.run_step(goal = 'Forward')
            print("PID control", control)
            control.steer = alpha
            if pred_brake is not None and pred_brake>=0.4 and speed>0.01: #pred_brake>=0.4 and speed > 0: for scene10 #pred_brake>=0.01 and speed > 0: for scene6 
                control.brake = 0.75#np.clip(np.float(max(min(pred_brake, 0.75),control.brake)),0.0,1.0)
                control.throttle = 0.0
                control.steer = 0.0
        else:
            control = carla.VehicleControl()
            pid_control = self._agent._local_planner.run_step(goal = 'Forward')
            if pred_throttle is not None:
                control.throttle = np.float(pred_throttle)
                control.brake = np.float(pred_brake)
                control.steer = np.float(pred_steer)
                print("Pred Control:", control.throttle, control.brake, control.steer)
                if control.brake >= control.throttle or control.brake>0.375:
                    control.throttle = 0.0
                    control.brake = 0.75
                else:
                    control.brake = 0.0
                if pid_control.brake > 0:
                    control.throttle = 0.0
                    control.brake = pid_control.brake
                control.throttle = np.clip(control.throttle,0.0,0.75)
                control.brake = np.clip(control.brake,0.0,0.75)
                control.steer = np.clip(control.steer,-1.0,1.0)

        pred_control = control
        #Control the agent using local planner according to predicted location
        #pred_control = None
        #if not Utils.InTriggerRegion_GlobalUtilFlag:
        #    pred_control = Utils.stop_control()
        #else:
        #    pred_control = self._agent._local_planner.run_step(goal = pred_location)
        #steer = self.turn_control.run_step(alpha, _cmd)
        #Throttle
        #Brake
        #FIXME: What will happen if the PID coeff are different
        print("Predited action:\t[{},\t{},\t{}]".format(pred_control.throttle, pred_control.brake, pred_control.steer))
        
        if not self._route_assigned:
            print("Setting up global plan")
            if self._global_plan:
                plan = []
                for transform, road_option in self._global_plan_world_coord:
                    print(transform.location, road_option)
                    wp = CarlaDataProvider.get_map().get_waypoint(transform.location)
                    plan.append((wp, road_option))
                    self._final_goal = np.array([transform.location.x,
                                                 transform.location.y,
                                                 transform.location.z])
                self._agent._local_planner.set_global_plan(plan)  # pylint: disable=protected-access
                self._route_assigned = True
                print("Global Plan set")
        
        #if self._final_goal is not None:
            #print("Current Location:", current_location, "Final_Goal:", self._final_goal)
            #dist2goal = np.linalg.norm(current_location-self._final_goal)
            #print("Distance to Goal: ", dist2goal)
        self._agent_control = pred_control
        return pred_control


    def postprocess(self, steer, throttle, brake):
        control = carla.VehicleControl()
        control.steer = np.clip(steer, -1.0, 1.0)
        control.throttle = np.clip(throttle, 0.0, 1.0)
        control.brake = np.clip(brake, 0.0, 1.0)
        control.manual_gear_shift = False

        return control


if __name__ == '__main__':
    agent = Lidar2DMinkowskiAgent("/home/cuijiaxun/Documents/AutoCast/wandb/run-20210210_004416-aqz93m5n/files/config.yaml")
    
