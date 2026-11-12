#!/bin/bash
#SBATCH --job-name=orbi_res_probe
#SBATCH --account=publicgrp
#SBATCH --partition=low
#SBATCH --qos=publicgrp-low-qos
#SBATCH --requeue
#SBATCH --time=0-03:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --array=0-19%10
#SBATCH --output=/quobyte/proteomics-grp/brett/scratch/orbi_res_probe_%A_%a.log
export DOTNET_ROOT=$HOME/.proteomics-pipeline/dotnet8
/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python /quobyte/proteomics-grp/brett/scratch/orbi_res_probe.py "$SLURM_ARRAY_TASK_ID" 20
