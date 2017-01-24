
# DO NOT RUN!  REVIEW EACH LINE AND VERIFY


# INSTALL esShardBalancer
cd ~
git clone https://github.com/klahnakoski/esShardBalancer.git
cd ~/esShardBalancer
git checkout master
sudo yum group install "Development Tools"
sudo yum install -y libffi-devel
sudo yum install -y openssl-devel

sudo /usr/local/bin/pip install ecdsa
sudo /usr/local/bin/pip install fabric
sudo /usr/local/bin/pip install -r requirements.txt


