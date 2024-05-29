V2V Perception


## Introduction
Optical sensors and learning algorithms for autonomous vehicles have dramatically advanced in the past few years. Nonetheless, the reliability of today's autonomous vehicles is hindered by the limited line-of-sight sensing capability and the brittleness of data-driven methods in handling extreme situations. With recent developments of telecommunication technologies, cooperative perception with vehicle-to-vehicle communications has become a promising paradigm to enhance autonomous driving in dangerous or emergency situations.


## Installation

First, clone the Coopernaut repo with AutoCastSim submodule.

```bash
git clone --recursive git@github.com:UT-Austin-RPL/Coopernaut.git
```
We provide a quickstart installation script in `scripts/install.sh`. 
In the root folder of this repo, run 

```bash
source scripts/install.sh #Requires CUDA11.0
```
or
```bash
source scripts/install-cu113.sh #Requires CUDA11.3
```
If you encounter errors or would like to follow individual steps, please refer to [INSTALL.md](docs/INSTALL.md) for more details.

If the the installation of `mosquitto` failed and you have sudo priority, please try the following command
```
sudo apt-get install mosquitto libopenblas-dev
```

## Quick Start

We provide a quick example to create evaluation trajectories of a trained Coopernaut model under the scenario 6:Overtaking, with two parallel threads(Make sure your GPU has a memory larger than 6GB, otherwise change the `CARLA_WORKERS` in the `scripts/quick_run.sh` to `1`). You can check the saved trajectories under `Coopernaut/result` directory. 
```bash
conda activate autocast
./scripts/quick_run.sh
```

