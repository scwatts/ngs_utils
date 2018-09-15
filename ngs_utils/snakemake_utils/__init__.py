import os
from datetime import datetime
from os.path import dirname, abspath, join
from ngs_utils import logger
from ngs_utils.file_utils import safe_mkdir
import tempfile
import yaml
from ngs_utils.call_process import run_simple


def package_path():
    return dirname(abspath(__file__))


def get_submit_script():
    return 'python ' + join(package_path(), 'submit')


def make_cluster_cmdl(log_dir, app_name=''):
    """ Generates cluster command line parameters for snakemake
    """
    from hpc_utils.hpc import get_loc
    loc = get_loc()
    if not loc.cluster:
        logger.critical(f'Automatic cluster submission is not supported for the machine "{loc.name}"')

    cluster_submitter = get_submit_script()
    timestamp = datetime.now().strftime('%Y_%m_%d_%H_%M_%S')
    cluster_cmdl = f' --cluster "{cluster_submitter} {timestamp} {log_dir} {app_name}"'

    # Also overriding jobscript?
    jobscript = loc.cluster.get('jobscript')
    if jobscript:
        fixed_jobscript = join(log_dir, 'jobscript.sh')
        with open(jobscript) as f_in, open(fixed_jobscript, 'w') as f_out:
            f_out.write(f_in.read().replace('{path}', os.environ["PATH"]))
        cluster_cmdl += f' --jobscript "{fixed_jobscript}"'

    return cluster_cmdl


def run_snakemake(smk_file, conf, jobs, output_dir, force_rerun=None, unlock=False):
    """ Runs snakemake
    """
    f = tempfile.NamedTemporaryFile(mode='wt', delete=False)
    yaml.dump(conf, f)
    f.close()

    cmd = (f'snakemake ' +
           f'--snakefile {smk_file} ' +
           f'--printshellcmds ' +
          (f'--directory {output_dir} ' if output_dir else '') +
           f'--configfile {f.name} ' +
           f'--jobs {jobs} ' +
          (f'--forcerun {force_rerun}' if force_rerun else '')
           )

    if unlock:
        print('* Unlocking previous run... *')
        run_simple(cmd + ' --unlock')
        print('* Now rerunning *')
    run_simple(cmd)
