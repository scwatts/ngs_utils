import re
from collections import defaultdict
import yaml
from os import listdir
from os.path import join, abspath, pardir, splitext, basename, dirname, realpath, isdir, isfile, exists

from ngs_utils.Sample import BaseSample
from ngs_utils.bam_utils import verify_bam
from ngs_utils.call_process import run, run_simple
from ngs_utils.config import load_yaml_config
from ngs_utils.file_utils import adjust_path, verify_dir, file_exists, safe_mkdir, verify_file, add_suffix
from ngs_utils.logger import critical, debug, info, err, warn
from ngs_utils.key_genes_utils import get_target_genes, is_small_target
import ngs_utils.variant_filtering as vf


CALLER_PRIORITY = ['ensemble', 'strelka2', 'vardict', 'gatk-haplotype']


class BcbioSample(BaseSample):
    def __init__(self, bcbio_project):
        BaseSample.__init__(self)
        self.bcbio_project = bcbio_project
        self.old_name = None
        self.raw_name = None
        self.project_tag = None
        self.genome_build = None
        self.sample_info = dict()

        self.is_rnaseq = None
        self.is_wgs = None
        self.min_allele_fraction = None
        self.coverage_interval = None

        self.variant_regions_bed = None
        self.sv_regions_bed = None
        self.coverage_bed = None

        self.bam = None
        self.counts_file = None

        self.variantcallers = []
        self.batch = None
        self.batch_names = []
        self.phenotype = None

    def get_name_for_files(self):  # In case if the sample if symlink from another project, and the name was changed in this one
        return self.old_name or self.name

    @staticmethod
    def parse_sample_ids(sample_info):
        description = str(sample_info['description']).replace('.', '_')

        batch_names = sample_info.get('metadata', dict()).get('batch')
        if isinstance(batch_names, int) or isinstance(batch_names, float):
            batch_names = str(batch_names)
        if isinstance(batch_names, str):
            batch_names = [batch_names]
        if batch_names:
            batch_names = [b.replace('.', '_') for b in batch_names if b]

        return description, batch_names

    @staticmethod
    def load_from_sample_info(sample_info, bcbio_project, exclude_samples=None,
                              include_samples=None, extra_batches=None, silent=False):
        # Get sample and batch names and exclude/include based on exclude_samples and include_samples
        description = str(sample_info['description']).replace('.', '_')

        batch_names = sample_info.get('metadata', dict()).get('batch')
        if isinstance(batch_names, int) or isinstance(batch_names, float):
            batch_names = str(batch_names)
        if isinstance(batch_names, str):
            batch_names = [batch_names]
        batch_names = [b.replace('.', '_') for b in batch_names if b]

        if exclude_samples:
            # Sample name
            if description in exclude_samples:
                if not silent: info(f'Skipping sample {description}')
                return None
            # Batch names
            if batch_names:
                filtered_batch_names = [b for b in batch_names if b not in exclude_samples]
                if not filtered_batch_names:
                    if not silent: info(f'Skipping sample {description} with batch info {", ".join(batch_names)}')
                    return None
                batch_names = filtered_batch_names

        if include_samples:
            # Sample name
            if description in include_samples:
                if not silent: info(f'Using sample {description} and all samples sharing batches {batch_names}')
            else:
                # Batch names
                if batch_names:
                    incl_batch_names = [b for b in batch_names if b in include_samples]
                    if incl_batch_names:
                        if not silent: info(f'Using sample {description} with batch info {", ".join(batch_names)}')
                    extr_batch_names = [b for b in batch_names if extra_batches and b in extra_batches]
                    if extr_batch_names and not incl_batch_names:
                        if not silent: info(f'Using sample {description} as it shares batches {extr_batch_names} with included samples')
                    incl_batch_names += extr_batch_names

                    if incl_batch_names:
                        batch_names = incl_batch_names
                    else:
                        return None

        # Creating BcbioSample object
        s = BcbioSample(bcbio_project)
        s.sample_info = sample_info
        if 'description_original' in sample_info:
            s.old_name = str(sample_info['description_original']).replace('.', '_')

        # Setting phenotype and batches
        s.phenotype = sample_info.get('metadata', dict()).get('phenotype', 'tumor')
        if not batch_names:
            batch_names = [s.get_name_for_files() + '-batch']
        if len(batch_names) > 1 and s.phenotype != 'normal':
            critical('Multiple batches for non-normal ' + s.phenotype + ' sample ' + s.name + ': ' + ', '.join(batch_names))
        s.batch_names = batch_names

        # Setting genome build based reference paths
        s.genome_build = sample_info['genome_build']
        s.variant_regions_bed = s.bcbio_project.config_path(val=sample_info['algorithm'].get('variant_regions'))
        s.sv_regions_bed = s.bcbio_project.config_path(val=sample_info['algorithm'].get('sv_regions')) or s.variant_regions_bed
        s.coverage_bed = s.bcbio_project.config_path(val=sample_info['algorithm'].get('coverage')) or s.sv_regions_bed
        if s.coverage_bed and not isfile(s.coverage_bed):
            if not silent:
                debug('coverage bed ' + str(s.coverage_bed) + ' not found. Looking relatively to genomes "basedir"')
            try:
                import az
            except ImportError:
                pass
            else:
                genome_cfg = az.get_refdata(s.genome_build)
                ref_basedir = genome_cfg.get('basedir')
                if not ref_basedir:
                    critical('coverage bed ' + str(s.coverage_bed) + ' not found and "basedir" not provided in system config')
                s.coverage_bed = join(ref_basedir, 'coverage', 'prioritize', s.coverage_bed) + '.bed'

        s.is_rnaseq = 'rna' in sample_info['analysis'].lower()
        s.min_allele_fraction = (1.0/100) * float(sample_info['algorithm'].get('min_allele_fraction', 1.0))
        if s.variant_regions_bed is None:
            s.coverage_interval = 'genome'
        else:
            s.coverage_interval = 'regional'
        s.is_wgs = s.coverage_interval == 'genome'

        if s._set_name_and_paths(
            name=description,
            variantcallers_data=sample_info['algorithm'].get('variantcaller'),
            ensemble='ensemble' in sample_info['algorithm'],
            silent=silent):
            return s
        else:
            return None

    def find_bam(self, silent=False):
        name = self.get_name_for_files()

        to_try = [
            '-ready.bam',
            '-ready.cram',
            '-sort.bam',
        ]
        for ext in to_try:
            fpath = adjust_path(join(self.dirpath, name + ext))
            if verify_file(fpath):
                return fpath

        input_file = self.sample_info['files']
        if not isinstance(input_file, str):
            input_file = input_file[0]
        if isinstance(input_file, str) and input_file.endswith('.bam'):
            debug('Bcbio was run from BAM input')
            if not input_file.startswith('/'):
                input_file = abspath(join(self.bcbio_project.work_dir, input_file))
            if verify_file(input_file):
                debug('Using BAM file from input YAML ' + input_file)
                return input_file
            else:
                debug('Input BAM file for sample ' + self.name + ' in YAML ' + input_file + ' does not exist')

        if not silent:
            warn('No BAM or CRAM file found for ' + self.name)


    def _set_name_and_paths(self, name, variantcallers_data, ensemble=False, silent=False):
        self.raw_name = name
        self.name = self.raw_name.replace('.', '_')
        self.dirpath = verify_dir(join(self.bcbio_project.final_dir, self.name))
        if not verify_dir(self.dirpath, silent=silent):
            if not silent:
                critical(f'Sample "{self.name}" specified in bcbio YAML is not found in the final directory '
                         f'{self.bcbio_project.final_dir}. Please check consistency between the YAML '
                         f'{self.bcbio_project.bcbio_yaml_fpath} and the directories in `final`: '
                         f'to every "description" value in YAML, there should be a corresponding folder with the '
                         f'same name in `final`. You can use `-e` option to exclude samples (comma-separated) '
                         f'from consideration, if you are sure that missing folders are expected.')
            else:
                return False
        self.var_dirpath = join(self.dirpath, BcbioProject.var_dir)

        self.bam = self.find_bam(silent=silent)

        if self.is_rnaseq:
            gene_counts = adjust_path(join(self.dirpath, self.get_name_for_files() + '-ready.counts'))
            if isfile(gene_counts) and verify_file(gene_counts):
                self.counts_file = gene_counts
            else:
                if not silent: warn('Counts for ' + self.name + ' not found')
        else:
            if variantcallers_data:
                self._set_variant_files(variantcallers_data, ensemble=ensemble)
            else:
                if not silent: warn('No variant callers set in config, skipping finding VCF files')
        return True

    def _set_variant_files(self, variantcallers_data, ensemble=False):
        if isinstance(variantcallers_data, dict):
            if 'germline' in variantcallers_data and self.phenotype == 'normal':
                self.variantcallers = variantcallers_data.get('germline')
            else:
                self.variantcallers = variantcallers_data.get('somatic')

        if isinstance(variantcallers_data, str):
            self.variantcallers = [variantcallers_data]
        elif isinstance(variantcallers_data, list):
            self.variantcallers = variantcallers_data

        if ensemble and len(self.variantcallers) > 1:
            self.variantcallers = ['ensemble'] + self.variantcallers

        if self.phenotype != 'germline' and self.phenotype != 'normal':
            self.bcbio_project.somatic_caller = next((c for c in CALLER_PRIORITY if c in self.variantcallers),
                                                     self.variantcallers[0])
        else:
            self.bcbio_project.germline_caller = next((c for c in CALLER_PRIORITY if c in self.variantcallers),
                                                      self.variantcallers[0])

    def find_mutation_files(self, passed=True, caller=None):
        return _find_mutation_files(join(self.dirpath, BcbioProject.varfilter_dir), passed, caller=caller)

    def find_raw_vcf(self, silent=False, caller=None):
        caller = caller or self.bcbio_project.somatic_caller
        vcf_fpath = None
        if self.batch and self.phenotype != 'normal':
            vcf_fpath = self.bcbio_project.find_vcf_file(self.batch.name, silent=silent, caller=caller)
        if not vcf_fpath:  # in sample dir?
            if not silent:
                debug('-')
                debug('Not found VCF in the datestamp dir, looking at the sample-level dir')
                debug('-')
            vcf_fpath = self.bcbio_project.find_vcf_file_from_sample_dir(
                self, silent=silent or self.phenotype == 'normal', caller=caller)
        return vcf_fpath

    def find_annotated_vcf(self, caller=None):
        caller = caller or self.bcbio_project.somatic_caller
        return verify_file(join(self.dirpath, BcbioProject.varannotate_dir,
                                self.name + '-' + caller + BcbioProject.anno_vcf_ending + '.gz'), silent=True)

    def find_filt_vcf(self, passed=False, caller=None):
        caller = caller or self.bcbio_project.somatic_caller
        path = join(self.dirpath, BcbioProject.varfilter_dir, self.name + '-' + caller +
                    ((BcbioProject.filt_vcf_ending + '.gz') if not passed else (BcbioProject.pass_filt_vcf_ending + '.gz')))
        return verify_file(path, silent=True)

    def find_mutation_file(self, passed=True, caller=None):
        caller = caller or self.bcbio_project.somatic_caller
        mut_fname = caller + '.' + vf.mut_file_ext
        mut_fpath = join(self.dirpath, BcbioProject.varfilter_dir, mut_fname)
        if passed:
            mut_fpath = add_suffix(mut_fpath, vf.mut_pass_suffix)
        return verify_file(mut_fpath, silent=True)

    def find_sv_vcf(self):
        return self.find_cnv_file(self.name + '-manta.vcf.gz') or \
               self.find_cnv_file(self.name + '-lumpy.vcf.gz')

    def find_sv_tsv(self):
        return self.find_cnv_file(self.name + '-sv-prioritize.tsv') or \
              (self.find_cnv_file(self.batch.name + '-sv-prioritize.tsv') if self.batch else None)

    def find_seq2c_calls(self):
        return self.find_cnv_file(self.name + '-seq2c.tsv') or \
              (self.find_cnv_file(self.batch.name + '-seq2c.tsv') if self.batch else None) or \
               verify_file(join(self.bcbio_project.date_dir, BcbioProject.cnv_dir, BcbioProject.seq2c_fname), silent=True) or \
               verify_file(join(self.bcbio_project.date_dir, BcbioProject.cnv_dir, 'Seq2C.tsv'), silent=True)

    def find_seq2c_coverage(self):
        return self.find_cnv_file(self.name + '-seq2c-coverage.tsv') or \
              (self.find_cnv_file(self.batch.name + '-seq2c-coverage.tsv') if self.batch else None) or \
               verify_file(join(self.bcbio_project.date_dir, BcbioProject.cnv_dir, 'seq2c-cov.tsv'), silent=True) or \
               verify_file(join(self.bcbio_project.date_dir, BcbioProject.cnv_dir, 'cov.tsv'), silent=True)

    def find_cnvkit_file(self):
        return self.find_cnv_file(self.name + '-cnvkit.cnr') or \
              (self.find_cnv_file(self.batch.name + '-cnvkit.cnr') if self.batch else None)

    def find_seq2c_file(self):
        return self.find_cnv_file(self.name + '-seq2c.tsv') or \
              (self.find_cnv_file(self.batch.name + '-seq2c.tsv') if self.batch else None)

    def find_cnv_file(self, fname):
        for fpath in [join(self.dirpath, fname),
                      join(self.dirpath, BcbioProject.cnv_dir, fname)]:
            if isfile(fpath):
                return verify_file(fpath, silent=True)

    def find_coverage_stats(self):
        sname = self.name
        dirpath = self.dirpath
        if self.phenotype == 'germline':
            sname = re.sub(r'-germline$', '', sname)
            dirpath = re.sub(r'-germline$', '', dirpath)
        return verify_file(join(dirpath, 'qc', 'coverage', sname + '_coverage.bed'), silent=True)

    def find_ngs_report(self, silent=False):
        return \
            verify_file(join(self.bcbio_project.date_dir, BcbioProject.reports_dir,
                             self.name + '.html'), silent=silent) or \
            verify_file(join(self.dirpath, BcbioProject.ngs_report_name,
                             BcbioProject.ngs_report_name + '.html'), silent=silent)

    def get_metric(self, names):
        if isinstance(names, str):
            names = [names]
        if not self.sample_info or not self.sample_info.get('metrics'):
            return None
        metrics = self.sample_info['metrics']
        val = None
        for k in metrics:
            if k.lower() in [n.lower() for n in names] and metrics[k] != 'NA':
                val = metrics[k]
        if val is None:
            err('Cannot find ' + ', '.join(names) + ' in metrics for ' + self.name)
        return val

    def get_avg_depth(self):
        return self.get_metric(['Avg_coverage', 'Avg_coverage_per_region'])

    def get_reads_count(self):
        return self.get_metric(['Total_reads', 'Total reads'])

    def get_usable_count(self):
        if self.get_metric('Usable_pct'):
            return int(self.get_reads_count() * self.get_metric('Usable_pct') / 100)

    def is_dedupped(self):
        return self.sample_info.get('algorithm', {}).get('mark_duplicates', False)


class Batch:
    def __init__(self, name=None):
        self.name = name
        self.normal = None
        self.tumor = None

    def is_paired(self):
        return self.normal and self.tumor

    def is_germline(self):
        return self.tumor.phenotype == 'germline'

    def __str__(self):
        return self.name


# class Caller:
#     def __init__(self, name=None, is_germline=False):
#         self.name = name
#         self.is_germline = is_germline
#         self.samples = []
#
#     def __str__(self):
#         return self.name


class NoConfigDirException(Exception):
    pass
class NoDateStampsException(Exception):
    pass
class MultipleDateStampsException(Exception):
    pass


class BcbioProject:
    varfilter_dir = 'varFilter'
    varannotate_dir = 'varAnnotate'
    cnv_dir = 'cnv'
    var_dir = 'var'
    ngs_report_name = 'ngs_report'
    reports_dir = 'reports'
    anno_vcf_ending = '.anno.vcf'
    filt_vcf_ending = '.anno.filt.vcf'
    pass_filt_vcf_ending = '.anno.filt.' + vf.mut_pass_suffix + '.vcf'
    seq2c_fname = 'seq2c.tsv'
    cnvkit_fname = 'cnvkit.tsv'
    evaluate_panel_dir = 'eval_panel'
    oncoprints_dir = 'oncoprints'

    ## RNAseq
    counts_names = []
    expression_dir = 'expression'

    multiqc_report_name = 'report.html'
    call_vis_name = 'call_vis.html'

    def __init__(self, input_dir=None, project_name=None, proc_name='postproc',
                 exclude_samples=None, include_samples=None, silent=False):
        self.config_dir = None
        self.final_dir = None
        self.date_dir = None
        self.log_dir = None
        self.postproc_log_dir = None
        self.work_dir = None
        self.bcbio_yaml_fpath = None

        self.var_dir = None
        self.raw_var_dir = None
        self.expression_dir = None

        self.versions = None
        self.programs = None

        self.samples = []
        self.batch_by_name = dict()
        self.samples_by_caller = defaultdict(list)  # (caller, is_germline) -> [samples]
        self.somatic_caller = 'ensemble'
        self.germline_caller = 'ensemble'

        self.variant_regions_bed = None
        self.sv_regions_bed = None          # "sv_regions" or "variant_regions"
        self.original_coverage_bed = None
        self.coverage_bed = None            # "coverage" or "sv_regions" or "variant_regions"

        self.project_name = None
        self.genome_build = None
        self.coverage_interval = None  # amplicon, regional, genome
        self.min_allele_fraction = None
        self.is_wgs = None
        self.is_rnaseq = None
        self.postproc_mqc_files = []

        self.silent = silent

        if input_dir:
            self.load_from_bcbio_dir(input_dir, project_name, proc_name,
                                     exclude_samples=exclude_samples, include_samples=include_samples)

    def set_project_level_dirs(self, bcbio_cnf, config_dir, project_name=None, final_dir=None, date_dir=None,
                               create_dirs=False, proc_name='postproc'):
        self.final_dir = self.set_final_dir(bcbio_cnf, config_dir, final_dir)
        if create_dirs: safe_mkdir(self.final_dir)

        self.project_name = self._set_project_name(self.final_dir, project_name)

        self.work_dir = abspath(join(self.final_dir, pardir, 'work'))
        if create_dirs: safe_mkdir(self.work_dir)

        self.date_dir = self._set_date_dir(bcbio_cnf, self.final_dir, date_dir, create_dir=create_dirs,
                                           silent=self.silent)
        self.log_dir = join(self.date_dir, 'log')
        self.postproc_log_dir = join(self.log_dir, proc_name)
        if create_dirs: safe_mkdir(self.postproc_log_dir)

        self.var_dir = join(self.date_dir, BcbioProject.var_dir)
        self.raw_var_dir = join(self.var_dir, 'raw')
        self.expression_dir = join(self.date_dir, BcbioProject.expression_dir)

        self.versions = verify_file(join(self.date_dir, 'data_versions.txt'), silent=True)
        self.programs = verify_file(join(self.date_dir, 'programs.txt'), silent=True)

    def load_from_bcbio_dir(self, input_dir, project_name=None, proc_name='postproc',
                            exclude_samples=None, include_samples=None):
        """
        Analyses existing bcbio folder.
        input_dir: root bcbio folder, or any other directory inside it
        """
        self.config_dir, self.final_dir, self.date_dir = detect_bcbio_dir(input_dir, silent=self.silent)
        bcbio_cnf, self.bcbio_yaml_fpath = load_bcbio_cnf(self.config_dir, silent=self.silent)
        self.set_project_level_dirs(bcbio_cnf, self.config_dir, project_name=project_name, final_dir=self.final_dir,
                                    date_dir=self.date_dir, proc_name=proc_name)
        self.set_samples(bcbio_cnf, exclude_samples=exclude_samples, include_samples=include_samples)
        self._load_bcbio_summary()
        # self._load_target_info()
        return self

    def set_samples(self, bcbio_cnf, exclude_samples=None, include_samples=None):
        debug('Reading sample details...')
        exclude_samples = [s.replace('.', '_') for s in exclude_samples] if exclude_samples else None
        include_samples = [s.replace('.', '_') for s in include_samples] if include_samples else None

        # First pass - just to get extra batch IDs that we need to include to have batches consistent
        extra_batches = set()
        if include_samples:
            for sample_info in bcbio_cnf['details']:
                sname, batch_names = BcbioSample.parse_sample_ids(sample_info)
                if sname in include_samples:
                    for b in batch_names:
                        if b not in (include_samples or []) and b not in (exclude_samples or []):
                            extra_batches.add(b)

        # Second pass - including/excluding, and creating BcbioSample objects
        for sample_info in bcbio_cnf['details']:
            s = BcbioSample.load_from_sample_info(
                sample_info,
                bcbio_project=self,
                exclude_samples=exclude_samples,
                include_samples=include_samples,
                extra_batches=extra_batches,
                silent=self.silent)
            if s:
                self.samples.append(s)

        if not self.samples:
            if exclude_samples:
                critical(f'Error: no samples left with the exclusion of batch/sample name(s): {", ".join(exclude_samples)}.'
                         f'Check the YAML file for available options: {self.bcbio_yaml_fpath}.')
            if include_samples:
                critical(f'Error: could not find a batch or a sample with the name(s): {", ".join(include_samples)}. '
                         f'Check the YAML file for available options: {self.bcbio_yaml_fpath}')
            critical(f'Error: could not parse any batch or samples in the bcbio project. '
                     f'Please check the bcbio YAML file: {self.bcbio_yaml_fpath}')

        not_found_samples = [s.name for s in self.samples if not s.bam]
        if not_found_samples:
            if not self.silent: warn(f'Warning: no BAM files not found for {len(not_found_samples)}/{len(self.samples)} samples')

        self.samples.sort(key=lambda _s: _s.key_to_sort())
        self.batch_by_name = self.update_batches(self.samples, self.silent)

        def _check_dup_props(prop, is_critical=False):
            _vals = set([s_.__dict__.get(prop) for s_ in self.samples])
            if len(_vals) > 1:
                (critical if is_critical else err)('Got different ' + prop + ' values in samples in ' + self.project_name)
            else:
                self.__dict__[prop] = _vals.pop()
        _check_dup_props('genome_build')
        _check_dup_props('variant_regions_bed')
        _check_dup_props('coverage_bed')
        _check_dup_props('sv_regions_bed')
        _check_dup_props('is_rnaseq')
        _check_dup_props('min_allele_fraction')
        _check_dup_props('is_wgs', is_critical=False)
        _check_dup_props('coverage_interval', is_critical=False)
        if self.is_rnaseq:
            debug('RNAseq')
        elif self.coverage_interval:
            debug('Coverage interval: ' + str(self.coverage_interval))

        for s in self.samples:
            for caller in s.variantcallers:
                self.samples_by_caller[(caller, s.phenotype == 'germline')].append(s)

        debug('Done loading bcbio project ' + self.project_name)

    def _load_bcbio_summary(self):
        fp = self.find_in_log('project-summary.yaml')
        if fp:
            with open(fp) as f:
                data = yaml.load(f)
            metrics_by_sample = dict()
            for s_data in data.get('samples', []):
                metrics_by_sample[s_data['description']] = s_data.get('summary', dict()).get('metrics')
            for s in self.samples:
                sname = s.name
                if s.phenotype == 'germline':
                    sname = re.sub(r'-germline$', '', s.name)
                s.sample_info['metrics'] = metrics_by_sample[sname]

    def config_path(self, val):
        if not val:
            return val
        full_path = adjust_path(join(self.config_dir, val))
        if exists(full_path):
            return full_path
        else:
            return val

    @staticmethod
    def _set_date_dir(bcbio_cnf, final_dir, date_dir, create_dir=False, silent=False):
        if not date_dir:
            fc_date = bcbio_cnf.get('fc_date')
            fc_name = bcbio_cnf.get('fc_name') or 'project'
            if fc_date:
                # Date dirpath is from bcbio and named after fc_name, not our own project name
                date_dir = join(final_dir, fc_date + '_' + fc_name)
                if not create_dir and not verify_dir(date_dir, silent=True):
                    critical('Error: no project directory of format {fc_date}_{fc_name} or {fc_name}_{fc_date}')
            else:
                if isdir(join(final_dir, 'project')):  # bcbio-CWL?
                    date_dir = join(final_dir, 'project')
                    if not silent: info('Using the datestamp dir from bcbio-CWL: ' + date_dir)
                else:
                    regexs = [fr'^\d\d\d\d-[01][0-9]-[0-3][0-9]_{fc_name}']
                    date_dirs = [join(final_dir, dirpath)
                                 for dirpath in listdir(final_dir)
                                 if any(re.match(regex, dirpath) for regex in regexs)]
                    if len(date_dirs) == 0:
                        raise NoDateStampsException('Error: no datestamp directory!')
                    elif len(date_dirs) == 1:
                        date_dir = date_dirs[0]
                    else:
                        dates = [(tuple(map(int, basename(d).split('_')[0].split('-'))), d) for d in date_dirs]
                        newest_date, newest_dir = sorted(dates, reverse=True)[0]
                        newest_dirs = [d_dir for d_dir in date_dirs if d_dir == newest_dir]
                        if len(newest_dirs) > 1:
                            raise MultipleDateStampsException(f'Error: multiple datestamp directory found, '
                               f'and can\'t select the most recent one because there are multiple latest dirs: {newest_dirs}')
                        date_dir = newest_dirs[0]

                    if not silent: info('Using the datestamp dir: ' + date_dir)
        if create_dir:
            safe_mkdir(date_dir)
        return date_dir

    @staticmethod
    def _set_project_name(final_dir, project_name=None):
        if not project_name:
            root_dir = dirname(final_dir)
            # path is like ../Bio_0031_Heme_MRL_DLBCL_IRAK4/bcbio_Dev_0079/final
            second_part = basename(root_dir)  # bcbio_Dev_0079
            bcbio_project_parent_dirname = basename(dirname(root_dir))  # Bio_0031_Heme_MRL_DLBCL_IRAK4
            project_name = bcbio_project_parent_dirname + '_' + second_part
        return project_name

    @staticmethod
    def set_final_dir(bcbio_cnf, config_dir, final_dir=None, create_dir=False):
        if final_dir:
            return final_dir
        elif 'upload' in bcbio_cnf and 'dir' in bcbio_cnf['upload']:
            final_dirname = bcbio_cnf['upload']['dir']
            final_dir = adjust_path(join(config_dir, final_dirname))
            if create_dir: safe_mkdir(final_dir)
            verify_dir(final_dir, 'upload directory specified in the bcbio config', is_critical=True)
        else:
            final_dir = abspath(join(config_dir, pardir, 'final'))
            if create_dir: safe_mkdir(final_dir)
            if not verify_dir(final_dir):
                critical('If final directory it is not named "final", please, specify it in the bcbio config.')
        return final_dir

    @staticmethod
    def update_batches(samples, silent=False):
        batch_by_name = {bn: Batch(bn) for bn in list(set([b for s in samples for b in s.batch_names]))}
        for sample in samples:
            for bn in sample.batch_names:
                batch_by_name[bn].name = bn
                sample.batch = batch_by_name[bn]
                if sample.phenotype == 'normal':
                    if batch_by_name[bn].normal:
                        critical('Multiple normal samples for batch ' + bn)
                    batch_by_name[bn].normal = sample
                else:
                    batch_by_name[bn].tumor = sample

        for batch in batch_by_name.values():
            if batch.normal and not batch.tumor:
                if not silent: info('Batch ' + batch.name + ' contains only normal, treating sample ' + batch.normal.name + ' as tumor')
                batch.normal.phenotype = 'tumor'
                batch.normal.batch = batch
                batch.tumor = batch.normal
                batch.normal = None

        # setting up batch properties
        for b in batch_by_name.values():
            b.tumor.normal_match = b.normal

        return batch_by_name

    def find_vcf_file(self, batch_name, silent=False, caller=None):
        caller = caller or self.somatic_caller
        vcf_fname = batch_name + '-' + caller + '.vcf'
        annot_vcf_fname = batch_name + '-' + caller + '-annotated.vcf'

        vcf_annot_fpath_gz = adjust_path(join(self.date_dir, annot_vcf_fname + '.gz'))  # in datestamp
        var_raw_vcf_annot_fpath_gz = adjust_path(join(self.raw_var_dir, annot_vcf_fname + '.gz'))  # in datestamp/var/raw

        vcf_fpath_gz = adjust_path(join(self.date_dir, vcf_fname + '.gz'))  # in datestamp
        var_vcf_fpath_gz = adjust_path(join(self.var_dir, vcf_fname + '.gz'))  # in datestamp/var
        var_raw_vcf_fpath_gz = adjust_path(join(self.raw_var_dir, vcf_fname + '.gz'))  # in datestamp/var/raw

        vcf_fpath = adjust_path(join(self.date_dir, vcf_fname))  # in datestamp
        var_vcf_fpath = adjust_path(join(self.var_dir, vcf_fname))  # in datestamp/var
        var_raw_vcf_fpath = adjust_path(join(self.raw_var_dir, vcf_fname))  # in datestamp/var/raw

        if isfile(vcf_annot_fpath_gz):
            verify_file(vcf_annot_fpath_gz, is_critical=True)
            if not silent: info('Found annotated VCF in the datestamp dir ' + vcf_annot_fpath_gz)
            return vcf_annot_fpath_gz
        else:
            debug('Not found annotated VCF in the datestamp dir ' + vcf_annot_fpath_gz)

        if isfile(var_raw_vcf_annot_fpath_gz):
            verify_file(var_raw_vcf_annot_fpath_gz, is_critical=True)
            if not silent: info('Found annotated VCF in the datestamp/var/raw dir ' + var_raw_vcf_annot_fpath_gz)
            return var_raw_vcf_annot_fpath_gz
        else:
            debug('Not found annotated VCF in the datestamp/var/raw dir ' + var_raw_vcf_annot_fpath_gz)

        if isfile(vcf_fpath_gz):
            verify_file(vcf_fpath_gz, is_critical=True)
            if not silent: info('Found VCF in the datestamp dir ' + vcf_fpath_gz)
            return vcf_fpath_gz
        else:
            debug('Not found VCF in the datestamp dir ' + vcf_fpath_gz)

        if isfile(var_raw_vcf_fpath_gz):
            verify_file(var_raw_vcf_fpath_gz, is_critical=True)
            if not silent: info('Found VCF in the datestamp/var/raw dir ' + var_raw_vcf_fpath_gz)
            return var_raw_vcf_fpath_gz
        else:
            debug('Not found VCF in the datestamp/var/raw dir ' + var_raw_vcf_fpath_gz)

        if isfile(vcf_fpath):
            verify_file(vcf_fpath, is_critical=True)
            if not silent: info('Found uncompressed VCF in the datestamp dir ' + vcf_fpath)
            return vcf_fpath
        else:
            debug('Not found uncompressed VCF in the datestamp dir ' + vcf_fpath)

        if isfile(var_raw_vcf_fpath):
            verify_file(var_raw_vcf_fpath, is_critical=True)
            if not silent: info('Found uncompressed VCF in the datestamp/var/raw dir ' + var_raw_vcf_fpath)
            return var_raw_vcf_fpath
        else:
            debug('Not found uncompressed VCF in the datestamp/var/raw dir ' + var_raw_vcf_fpath)

        if isfile(var_vcf_fpath_gz):
            verify_file(var_vcf_fpath_gz, is_critical=True)
            if not silent: info('Found VCF in the datestamp/var dir ' + var_vcf_fpath_gz)
            return var_vcf_fpath_gz
        else:
            debug('Not found VCF in the datestamp/var dir ' + var_vcf_fpath_gz)

        if isfile(var_vcf_fpath):
            verify_file(var_vcf_fpath, is_critical=True)
            if not silent: info('Found uncompressed VCF in the datestamp/var dir ' + var_vcf_fpath)
            return var_vcf_fpath
        else:
            debug('Not found uncompressed VCF in the datestamp/var dir ' + var_vcf_fpath)

        if not silent:
            warn('Warning: no VCF found for batch ' + batch_name + ', ' + caller + ', gzip or '
                'uncompressed version in the datestamp directory.')
        return None

    @staticmethod
    def find_vcf_file_from_sample_dir(sample, silent=False, caller=None):
        caller = caller or sample.bcbio_project.somatic_caller
        vcf_fname = sample.get_name_for_files() + '-' + caller + '.vcf'

        sample_var_dirpath = join(sample.dirpath, 'var')
        vcf_fpath_gz = adjust_path(join(sample.dirpath, vcf_fname + '.gz'))  # in var
        var_vcf_fpath_gz = adjust_path(join(sample_var_dirpath, vcf_fname + '.gz'))  # in var
        var_raw_vcf_fpath_gz = adjust_path(join(sample_var_dirpath, 'raw', vcf_fname + '.gz'))  # in var
        vcf_fpath = adjust_path(join(sample.dirpath, vcf_fname))
        var_vcf_fpath = adjust_path(join(sample_var_dirpath, vcf_fname))  # in var
        var_raw_vcf_fpath = adjust_path(join(sample_var_dirpath, 'raw', vcf_fname))  # in var

        if isfile(vcf_fpath_gz):
            verify_file(vcf_fpath_gz, is_critical=True)
            if not silent: info('Found VCF ' + vcf_fpath_gz)
            return vcf_fpath_gz
        else:
            debug('Not found VCF ' + vcf_fpath_gz)

        if isfile(var_vcf_fpath_gz):
            verify_file(var_vcf_fpath_gz, is_critical=True)
            if not silent: info('Found VCF in the var/ dir ' + var_vcf_fpath_gz)
            return var_vcf_fpath_gz
        else:
            debug('Not found VCF in the var/ dir ' + var_vcf_fpath_gz)

        if isfile(var_raw_vcf_fpath_gz):
            verify_file(var_raw_vcf_fpath_gz, is_critical=True)
            if not silent: info('Found VCF in the var/raw/ dir ' + var_raw_vcf_fpath_gz)
            return var_raw_vcf_fpath_gz
        else:
            debug('Not found VCF in the var/raw/ dir ' + var_raw_vcf_fpath_gz)

        if isfile(vcf_fpath):
            verify_file(vcf_fpath, is_critical=True)
            if not silent: info('Found uncompressed VCF ' + vcf_fpath)
            return vcf_fpath
        else:
            debug('Not found uncompressed VCF ' + vcf_fpath)

        if isfile(var_vcf_fpath):
            verify_file(var_vcf_fpath, is_critical=True)
            if not silent: info('Found uncompressed VCF in the var/ dir ' + var_vcf_fpath)
            return var_vcf_fpath
        else:
            debug('Not found VCF in the var/ dir ' + var_vcf_fpath)

        if isfile(var_raw_vcf_fpath):
            verify_file(var_raw_vcf_fpath, is_critical=True)
            if not silent: info('Found uncompressed VCF in the var/raw/ dir ' + var_raw_vcf_fpath)
            return var_raw_vcf_fpath
        else:
            debug('Not found VCF in the var/raw/ dir ' + var_raw_vcf_fpath)

        if not silent:
            warn('Warning: no VCF found for ' + sample.name + ' (' + caller + '), gzip or uncompressed version in and outside '
                'the var directory. Phenotype is ' + str(sample.phenotype))
        return None

    def find_seq2c_file(self):
        return verify_file(join(self.date_dir, BcbioProject.cnv_dir, BcbioProject.seq2c_fname), silent=True) or \
               verify_file(join(self.date_dir, BcbioProject.cnv_dir, 'Seq2C.tsv'), silent=True)

    def find_seq2c_filt_file(self):
        return verify_file(join(self.date_dir, BcbioProject.cnv_dir,
                                add_suffix(BcbioProject.seq2c_fname, 'filt')), silent=True)

    def find_seq2c_coverage(self):
        return verify_file(join(self.date_dir, BcbioProject.cnv_dir, 'seq2c-cov.tsv'), silent=True)

    def find_cnvkit_file(self):
        return verify_file(join(self.date_dir, BcbioProject.cnv_dir, BcbioProject.cnvkit_fname), silent=True)

    def find_cnvkit_filt_file(self):
        return verify_file(join(self.date_dir, BcbioProject.cnv_dir,
                                add_suffix(BcbioProject.cnvkit_fname, 'filt')), silent=True)

    def get_cnv_caller(self):
        seq2c_file = self.find_seq2c_filt_file()
        cnvkit_file = self.find_cnvkit_filt_file()
        if not seq2c_file or (cnvkit_file and self.is_wgs):
            return 'CNVkit'
        else:
            return 'Seq2C'

    def find_cnv_filt_file(self):
        caller = self.get_cnv_caller()
        if caller == 'Seq2C':
            return self.find_seq2c_filt_file()
        else:
            return self.find_cnvkit_filt_file()

    def find_multiqc_report(self):
        for fpath in [
            join(self.date_dir, BcbioProject.multiqc_report_name),
            join(self.date_dir, 'multiqc_postproc', 'multiqc_report.html'),
        ]:
            if verify_file(fpath, silent=True):
                return fpath

    def find_mutation_files(self, passed=True, caller=None, is_germline=False):
        return _find_mutation_files(self.var_dir, passed=passed, caller=caller, is_germline=is_germline)

    def find_in_log(self, fname, is_critical=False, silent=True):
        options = [join(self.log_dir, fname),
                   join(self.date_dir, fname)]
        for fpath in options:
            if isfile(fpath):
                return fpath
        if is_critical:
            critical('Log file not found as ' + ', '.join(options))
        elif not silent:
            err('Log file not found as ' + ', '.join(options))

    def get_target_genes(self, get_key_genes_file=None):
        return get_target_genes(self.genome_build, self.coverage_bed,
                                get_key_genes_file=get_key_genes_file)

    def is_small_target(self):
        return is_small_target(self.coverage_bed)


def _find_mutation_files(base_dir, passed=True, caller=None, is_germline=False):
    assert caller
    mut_fname = caller + '.' + vf.mut_file_ext
    mut_fpath = join(base_dir, mut_fname)
    single_mut_fpath = add_suffix(mut_fpath, vf.mut_single_suffix)
    paired_mut_fpath = add_suffix(mut_fpath, vf.mut_paired_suffix)
    fpaths = [mut_fpath, single_mut_fpath, paired_mut_fpath]
    if passed:
        fpaths = [add_suffix(p, vf.mut_pass_suffix) for p in fpaths]
    return [p for p in fpaths if verify_file(p, silent=True)]


def detect_bcbio_dir(input_dir, silent=False):
    """
    :param input_dir: `config` dir, or `final` dir, or datestamp dir, or the directory root to `final`
    :return: (config_dir, final_dir, date_dir)
    """
    config_dir, final_dir, date_dir = None, None, None

    input_dir = abspath(input_dir)

    # We are inside `*final*`
    if 'final' in basename(input_dir):  # allow prefixes and postfixes
        final_dir = input_dir
        root_dir = dirname(final_dir)
        config_dir = join(root_dir, 'config')
        if not isdir(config_dir):
            err(f'Are you running on a bcbio output?\n'
                f'The input folder appear to be `final` ({input_dir}), '
                f'however can\'t find `config` directory at the same level ({config_dir})')
            raise NoConfigDirException('No config dir')

    # We are inside `config`
    elif basename(input_dir) == 'config':
        config_dir = input_dir

    # We are in a parent dir to `config` (and possibly `final`, called otherwise)
    elif isdir(join(input_dir, 'config')):
        config_dir = join(input_dir, 'config')

    # We are inside a date dir
    elif isdir(abspath(join(input_dir, pardir, pardir, 'config'))):
        final_dir = abspath(join(input_dir, pardir))
        root_dir = abspath(join(input_dir, pardir, pardir))
        config_dir = abspath(join(root_dir, 'config'))

        # if 'final' not in basename(final_dir):
        #     err(f'Are you running on a bcbio output?\n'
        #         f'Found config directory 2 level up at {config_dir}, assuming your input {input_dir} '
        #         f'is a datestamp directory. However, the parent directory is not called `*final*`')
        #     raise NoConfigDirException('No final dir')

    else:
        if not silent:
            err(f'Are you running on a bcbio output?\n'
                f'{input_dir} is not `config` or `*final*`, and '
                f'can\'t find a `config` directory at {join(input_dir, "config")}, or {abspath(join(input_dir, pardir, "config"))}.'
                f'Make sure that you changed to a bcbio root or final directory, or provided it as a first argument.')
        raise NoConfigDirException('No config dir')

    if not silent:
        if not silent:
            info(f'Bcbio config directory: ' + config_dir)
        if final_dir:
            if not silent: info('"final" directory: ' + final_dir)
            if date_dir:
                if not silent: info('"datestamp" directory: ' + date_dir)

    return config_dir, final_dir, date_dir


def load_bcbio_cnf(config_dir, silent=False):
    all_yamls = [
        abspath(join(config_dir, fname))
        for fname in listdir(config_dir)
        if fname.endswith('.yaml')]
    if len(all_yamls) == 0:
        critical('No YAML file in the config directory.')

    bcbio_yamls = []
    for fpath in all_yamls:
        if not fpath.endswith('-template.yaml'):
            if 'details' in load_yaml_config(fpath):
                bcbio_yamls.append(fpath)
    if len(bcbio_yamls) == 0:
        critical('No bcbio YAMLs found in the config directory: ' + config_dir +
                 ' (only ' + ', '.join(map(basename, all_yamls)) +
                 ' which do not have the "details" section)')
    if len(bcbio_yamls) > 1:
        critical('More than one bcbio YAML file found in the config directory ' +
                 config_dir + ': ' + ' '.join(bcbio_yamls))
    yaml_fpath = bcbio_yamls[0]
    if not silent: info('Using bcbio YAML config: ' + yaml_fpath)
    return load_yaml_config(yaml_fpath), yaml_fpath


def _normalize(name):
    return name.lower().replace('_', '').replace('-', '')


def ungzip_if_needed(cnf, fpath, silent=False):
    if fpath.endswith('.gz'):
        fpath = fpath[:-3]
    if not file_exists(fpath) and file_exists(fpath + '.gz'):
        gz_fpath = fpath + '.gz'
        cmdline = 'gunzip -c {gz_fpath} > {fpath}'.format(**locals())
        res = run_simple(cmdline)
        if not silent: info()
        if not res:
            return None
    return fpath

 
# def _parse_coveragre_interval(bcbio_project, sample_name):
#     interval = None
#     target_info_file = join(bcbio_project.date_dir, 'multiqc', 'report', 'metrics', 'target_info.yaml')
#     if not isfile(target_info_file):
#         target_info_file = join(bcbio_project.date_dir, 'log', 'multiqc_bcbio', 'report', 'metrics', 'target_info.yaml')
#     if isfile(target_info_file):
#         debug('Parsing ' + target_info_file + ' for coverage_interval')
#         with open(target_info_file) as f:
#             d = yaml.load(f)
#             if 'coverage_interval' in d:
#                 interval = d['coverage_interval']
#             else:
#                 debug('Not found coverage_interval in ' + target_info_file)
#
#     if not interval:
#         bcbio_log = bcbio_project.find_in_log('bcbio-nextgen.log', silent=False)
#         if not bcbio_log:
#             debug('Not found bcbio-nextgen.log')
#         else:
#             debug('Parsing coverage_interval from log')
#             pattern = r'.* (?P<sample>.+): Assigned coverage as \'(?P<interval>.+)\''
#             with open(bcbio_log) as f:
#                 for l in f:
#                     m = re.match(pattern, l)
#                     if m and len(m.groups()) == 2:
#                         if m.group('sample') == sample_name:
#                             interval = m.group('interval')
#             if not interval:
#                 err('Coverage interval info is not found in ' + bcbio_log)
#     return interval
