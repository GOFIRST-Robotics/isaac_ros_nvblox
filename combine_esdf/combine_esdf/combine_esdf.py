import rclpy
from rclpy.node import Node

from nvblox_msgs.msg import DistanceMapSlice
from sensor_msgs.msg import PointCloud2

import numpy as np

class combine_esdf(Node):

    def __init__(self):
        super().__init__('combine_esdf')

        self.declare_parameter('combine_pointcloud', False)
        self.declare_parameter('invert_below_ground', False)
        self.declare_parameter('combine_costmap', False)
        self.declare_parameter('above_ground_denoise_level', 0.5)

        self.above_ground_denoise_level = self.get_parameter('above_ground_denoise_level').value

        if self.get_parameter('combine_costmap').value:
            self.costmap_publisher = self.create_publisher(DistanceMapSlice, '/nvblox_node/combined_esdf', 10)
            self.get_logger().info("Costmap combining enabled")
            self.above_ground_subscriber = self.create_subscription(
                DistanceMapSlice,
                '/nvblox_node/static_map_slice',
                self.above_ground_callback,
                1)
            
            self.below_ground_subscriber = self.create_subscription(
                DistanceMapSlice,
                '/nvblox_node/static_map_slice2',
                self.below_ground_callback,
                1)
        
        if self.get_parameter('combine_pointcloud').value:
            self.get_logger().info("Pointcloud combining enabled")
            self.pointcloud_publisher = self.create_publisher(PointCloud2, '/nvblox_node/combined_esdf_pointcloud', 10)
            self.above_ground_pointcloud_subscriber = self.create_subscription(
                PointCloud2,
                '/nvblox_node/static_esdf_pointcloud',
                self.above_ground_pointcloud_callback,
                10)
            
            self.below_ground_pointcloud_subscriber = self.create_subscription(
                PointCloud2,
                '/nvblox_node/static_esdf_pointcloud2',
                self.below_ground_pointcloud_callback,
                10)
        
        self.above_ground_costmap = None
        self.below_ground_costmap = None
        self.below_origin = None

        self.above_ground_pointcloud = None
        self.below_ground_pointcloud = None


    # Costmap combining logic.
    # This is both the callback for the above ground costmap, as well as the combiner, and publisher of the combined costmaps. 
    def above_ground_callback(self, msg):
        if (len(msg.data) == 0):
            return
        
        if (self.below_ground_costmap is None):
            self.costmap_publisher.publish(msg)
            return
        
        self.above_ground_costmap = np.array(msg.data).reshape(msg.height, msg.width)
        origin = (msg.origin.x, msg.origin.y)

        if (self.below_ground_costmap.shape[0] > self.above_ground_costmap.shape[0]) or (self.below_ground_costmap.shape[1] > self.above_ground_costmap.shape[1]):
            self.costmap_publisher.publish(msg)
            return
        
        # Calculating the points that align between the two costmaps. The x and y are flipped intentionally.
        # convert that difference to matrix indices by dividing by resolution, and then saving that value.
        offset_x1 = abs(int((origin[1] - self.below_origin[1]) / msg.resolution))
        offset_y1 = abs(int((origin[0] - self.below_origin[0]) / msg.resolution))
        offset_x2 = ((self.above_ground_costmap.shape[0] - self.below_ground_costmap.shape[0]) - offset_x1)
        offset_y2 = ((self.above_ground_costmap.shape[1] - self.below_ground_costmap.shape[1]) - offset_y1)
        
        # To invert or not to invert, that is the question. 
        if self.get_parameter('invert_below_ground').value:
            # Invert the below ground costmap. This is because the below ground costmap is inverted.
            self.below_ground_costmap[self.below_ground_costmap == 1000] = 0
            self.below_ground_costmap = np.full(self.below_ground_costmap.shape, 2) - self.below_ground_costmap
            self.below_ground_costmap = 2 - self.sigmoid(self.below_ground_costmap)
        else:
            # if you don't want to invert, use this
            condition2 = self.below_ground_costmap <= 2
            self.below_ground_costmap[condition2] = 0
            # Filter danger out from the regular terrain
            self.above_ground_costmap[self.above_ground_costmap >= self.above_ground_denoise_level] = 2

        if (self.above_ground_costmap[offset_x1:-offset_x2, offset_y1:-offset_y2].shape != self.below_ground_costmap.shape):
            self.costmap_publisher.publish(msg)
            return
        self.above_ground_costmap[offset_x1:-offset_x2, offset_y1:-offset_y2] = np.minimum((self.above_ground_costmap[offset_x1:-offset_x2, offset_y1:-offset_y2]), self.below_ground_costmap)

        # Convert the costmap to a message and publish it.
        msg.data = self.above_ground_costmap.flatten().tolist()
        self.costmap_publisher.publish(msg)
        

    # Below ground costmap callback; sets the variables for below ground costmap and origin.
    # Easily replicable for more below ground costmaps. Stack the costmaps into a 3D array
    # Probably useful to move the inversion logic to this function if this is the case.
    def below_ground_callback(self, msg):
        if (len(msg.data) == 0):
            return   
        self.below_ground_costmap = np.array(msg.data).reshape(msg.height, msg.width)
        self.below_origin = (msg.origin.x, msg.origin.y)


    # POINTCLOUD COMBINING LOGIC
    # Performance is garbage, but unnecessary for nav2 purposes.
    def above_ground_pointcloud_callback(self, msg):
        if (len(msg.data) == 0 or self.below_ground_pointcloud is None):
            self.pointcloud_publisher.publish(msg)
            return
        
        # The data array is a list of uint8's that we need to convert to float32's
        # it is read (X, Y, Z, Intensity) for each point in the pointcloud.
        # See PointCloud2 documentation for more info.
        above_ground_float_values = np.frombuffer(msg.data, dtype=np.uint8).view(dtype=np.float32)
        below_ground_cloud = self.below_ground_pointcloud.view(dtype=np.float32) # this is .frombuffered in its callback

        # Constructs a 'for loop' using index slicing and loop unrolling
        # +0 = x, +1 = y, +2 = z, +3 = intensity
        slice_indices = np.arange(0, len(above_ground_float_values), 4)
        slice_indices_below = np.arange(0, len(below_ground_cloud), 4)
        
        x_above_ground = above_ground_float_values[slice_indices]
        y_above_ground = above_ground_float_values[slice_indices + 1]
        data_above_ground = above_ground_float_values[slice_indices + 3]
        above_coords = np.array([x_above_ground, y_above_ground]).T # Merge the x and y coordinates into a 2D array -> [x, y]

        x_below_ground = below_ground_cloud[slice_indices_below] # Takes every 4th element, and 
        y_below_ground = below_ground_cloud[slice_indices_below + 1]
        data_below_ground = below_ground_cloud[slice_indices_below + 3]
        below_coords = np.array([x_below_ground, y_below_ground]).T # Merge the x and y coordinates into a 2D array -> [x, y]
        
        data_below_ground[data_below_ground <=2] = 0  # extremifying the hole values
        # data_below_ground = (2 - data_below_ground)  # Invert the esdf

        # Find the indices where the above ground and below ground pointclouds overlap
        data_indices = np.where(np.all(np.round(above_coords[:,None], 3) == np.round(below_coords[None, :], 3), axis=-1))
        
        # Combine the intensity values where the coordinates overlap.
        data_above_ground[data_indices[0]] = np.minimum(data_above_ground[data_indices[0]], data_below_ground[data_indices[1]])
        above_ground_float_values[slice_indices+3] = data_above_ground
        
        msg.data = np.frombuffer(above_ground_float_values.tobytes(), dtype=np.uint8).tolist()
        self.pointcloud_publisher.publish(msg)


    def below_ground_pointcloud_callback(self, msg):
        if (len(msg.data) == 0):
            return
        self.below_ground_pointcloud = np.frombuffer(msg.data, dtype=np.uint8)

def main(args=None):
    rclpy.init(args=args)
    node = combine_esdf()
    node.get_logger().info("Combine ESDF node started")
    rclpy.spin(node)

    node.destroy_node()
    rclpy.shutdown()
