#!/bin/bash

#SBATCH -J mspass           # Job name
#SBATCH -o mspass.o%j       # Name of stdout output file
#SBATCH -p skx-dev          # Queue (partition) name
#SBATCH -N 1               # Total # of nodes (must be 1 for serial)
#SBATCH -n 1               # Total # of mpi tasks (should be 1 for serial)
#SBATCH -t 02:00:00        # Run time (hh:mm:ss)
#SBATCH -A MsPASS       # Allocation name (req'd if you have more than 1)

# working directory
WORK_DIR=$SCRATCH/mspass/single_workdir
# directory where contains docker image
MSPASS_CONTAINER=$WORK2/mspass/mspass_latest.sif

# command that start the container
SING_COM="singularity run --home $WORK_DIR $MSPASS_CONTAINER"

module unload xalt
module load tacc-singularity

module list
pwd
date

NODE_HOSTNAME=`hostname -s`
LOGIN_PORT=`echo $NODE_HOSTNAME | perl -ne 'print (($2+1).$3.$1) if /c\d(\d\d)-(\d)(\d\d)/;'`
echo "got login node port $LOGIN_PORT"

# create reverse tunnel port to login nodes.  Make one tunnel for each login so the user can just
# connect to stampede.tacc
for i in `seq 4`; do
    ssh -q -f -g -N -R $LOGIN_PORT:$NODE_HOSTNAME:8888 login$i
done
echo "Created reverse ports on Stampede2 logins"

mkdir -p $WORK_DIR
cd $WORK_DIR

# database should be deployed as 'scratch' or 'tmp' mode
DB_PATH='scratch'

SINGULARITYENV_MSPASS_DB_PATH=$DB_PATH \
SINGULARITYENV_MSPASS_WORK_DIR=$WORK_DIR $SING_COM