pip install --upgrade pip 
pip install numpy==1.17 torch==1.3.1 torchvision==0.4.2 tensorboard==1.14.0 pyyaml opencv-python scikit-image pandas imageio tqdm
cd codes/models/archs/dcn
python setup.py develop
cd /workspace/DynaVSR
apt-get update && apt install -y libgl1-mesa-glx
apt-get install -y libgtk2.0-dev

python3 codes/test_dynavsr.py -opt /codes/options/test/EDVR/EDVR_1bn.yml
python3 codes/test_dynavsr_V.py -opt /codes/options/test/EDVR/EDVR_1bn_V.yml
python3 codes/test_dynavsr.py -opt /codes/options/test/EDVR/EDVR_1gn.yml
python3 codes/test_dynavsr_V.py -opt /codes/options/test/EDVR/EDVR_1gn_V.yml

python3 codes/test_dynavsr.py -opt /codes/options/test/EDVR/EDVR_2bn.yml
python3 codes/test_dynavsr_V.py -opt /codes/options/test/EDVR/EDVR_2bn_V.yml
python3 codes/test_dynavsr.py -opt /codes/options/test/EDVR/EDVR_2gn.yml
python3 codes/test_dynavsr_V.py -opt /codes/options/test/EDVR/EDVR_2gn_V.yml

python3 codes/test_dynavsr.py -opt /codes/options/test/EDVR/EDVR_3bn.yml
python3 codes/test_dynavsr_V.py -opt /codes/options/test/EDVR/EDVR_3bn_V.yml
python3 codes/test_dynavsr.py -opt /codes/options/test/EDVR/EDVR_3gn.yml
python3 codes/test_dynavsr_V.py -opt /codes/options/test/EDVR/EDVR_3gn_V.yml