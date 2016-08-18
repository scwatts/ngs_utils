from os.path import dirname, join, abspath, isdir, isfile
import os

from Utils.file_utils import file_exists, which
from Utils.logger import critical, err


def get_dirpath():
    return abspath(join(dirname(__file__), 'bedtools2'))


def get_executable_path():
    return join(get_dirpath(), 'bin', 'bedtools')


def find_executable():
    exec_fpath = get_executable_path()
    if not file_exists(exec_fpath):
        exec_fpath_in_path = which('bedtools')
        if exec_fpath_in_path:
            err('BedTools compilation failed, using bedtools in $PATH: ' + exec_fpath_in_path + '\n')
        else:
            critical('Error: could not find BedTools executable at ' + exec_fpath + ' or in $PATH')
    return exec_fpath


os.environ['PATH'] = dirname(find_executable()) + ':' + os.environ['PATH']
# noinspection PyUnresolvedReferences
from pybedtools import BedTool
