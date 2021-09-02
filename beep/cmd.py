"""
The entire BEEP CLI.

Guiding principles of how the CLI works:
 - Errors are thrown (stderr) if the *command itself* is bad.
 - Errors on individual files are caught during processing and
   reported via logging or status json.

The main "beep" command with no subcommand can specify where
and how to log all output and results (i.e., reporting).

The subcommands themselves specify where and how to process
actual BEEP operations such as structuring, featurization,
protocol generation, and running models.

"""

import os
import ast
import sys
import time
import fnmatch
import hashlib
import logging
import datetime
import traceback
import importlib

import click
from monty.serialization import dumpfn

from beep import (
    logger,
    BEEP_PARAMETERS_DIR,
    S3_CACHE,
    formatter_jsonl,
    __version__
)
from beep.structure.cli import auto_load, auto_load_processed
from beep.features.base import BEEPFeaturizer, BEEPFeaturizationError
from beep.features.core import (
    HPPCResistanceVoltageFeatures,
    DeltaQFastCharge,
    TrajectoryFastCharge,
    CycleSummaryStats,
    DiagnosticProperties,
    DiagnosticSummaryStats
)
from beep.features.intracell_losses import (
    IntracellCycles,
    IntracellFeatures
)
from beep.utils.s3 import list_s3_objects, download_s3_object
from beep.validate import BeepValidationError

CLICK_FILE = click.Path(file_okay=True, dir_okay=False, writable=False, readable=True)
CLICK_DIR = click.Path(file_okay=False, dir_okay=True, writable=True, readable=True)
STRUCTURED_SUFFIX = "-structured"
FEATURIZED_SUFFIX = "-featurized"


class ContextPersister:
    """
    Class to hold persisting objects for downstream
    BEEP tasks.
    """
    def __init__(
            self,
            cwd=None,
            run_id=None,
            tags=None,
            output_status_json=None,
            halt_on_error=None

    ):
        self.cwd = cwd
        self.run_id = run_id
        self.tags = tags
        self.output_status_json = output_status_json
        self.halt_on_error = halt_on_error


def add_suffix(full_path, output_dir, suffix, modified_ext=None):
    """
    Add structured filename suffixes.

    Args:
        full_path:
        output_dir:
        suffix:
        modified_ext:

    Returns:

    """
    basename = os.path.basename(full_path)
    stripped_basename, ext = os.path.splitext(basename)
    if modified_ext:
        ext = modified_ext
    new_basename = stripped_basename + suffix + ext
    return os.path.join(
        output_dir,
        new_basename
    )


def add_metadata_to_status_json(status_dict, run_id, tags):
    """Add some basic metadata to the status json.

    Args:
        status_dict (dict): Dictionary which will be written to status hson.
        run_id (int): Run id of this operation.
        tags ([str]): List of short string tags tagging an operation.

    Returns:
        (dict): Dictionary including BEEP metadata
    """
    metadata = {
        "beep_verison": __version__,
        "op_datetime_utc": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "run_id": run_id,
        "tags": tags
    }
    status_dict["metadata"] = metadata
    return status_dict


def md5sum(filename):
    """
    Get md5 sum hash of a file.

    Args:
        filename (str): Name of the file.

    Returns:
        (str) Hash digest h.
    """
    with open(filename, "rb") as f:
        d = f.read()
        h = hashlib.md5(d).hexdigest()
    return h


@click.group(invoke_without_command=False)
@click.option(
    "--log-file",
    "-l",
    type=click.Path(file_okay=True, dir_okay=False, writable=True, readable=True),
    multiple=False,
    help="File to log formatted json to. Log will still be output in human "
         "readable form to stdout, but if --log-file is specified, it will "
         "be additionally logged to a jsonl (json-lines) formatted file.",
)
@click.option(
    "--run-id",
    "-r",
    type=click.INT,
    multiple=False,
    help="An integer run_id which can be optionally assigned to this run. "
         "It will be output in the metadata status json for any subcommand "
         "if the status json is enabled."
)
@click.option(
    "--tags",
    "-t",
    type=click.STRING,
    multiple=True,
    help="Add optional tags to the status json metadata. Can be later used for"
         "large-scale queries on database data about sets of BEEP runs. Example:"
         "'experiments_for_kristin'."
)
@click.option(
    '--output-status-json',
    '-s',
    type=CLICK_FILE,
    multiple=False,
    help="File to output with JSON info about the states of "
         "files which have had any beep subcommand operation"
         "run on them (e.g., structuring). Contains comprehensive"
         "info about the success of the operation for all files."
         "1 status json = 1 operation."
)
@click.option(
    '--halt-on-error',
    is_flag=True,
    default=False,
    help="Set to halt BEEP if critical featurization "
         "errors are encountered on any file with any featurizer. "
         "Otherwise, logs critical errors to the status json.",
)
@click.pass_context
def cli(ctx, log_file, run_id, tags, output_status_json, halt_on_error):
    """
    Base command for all BEEP subcommands. Sets CWD and persistent
    context.
    """
    ctx.ensure_object(ContextPersister)
    cwd = os.path.abspath(os.getcwd())
    ctx.obj.cwd = cwd
    ctx.obj.tags = tags
    ctx.obj.run_id = run_id
    ctx.obj.output_status_json = output_status_json
    ctx.obj.halt_on_error = halt_on_error


    if log_file:
        hdlr = logging.FileHandler(log_file, "a")
        hdlr.setFormatter(formatter_jsonl)
        logger.addHandler(hdlr)


@cli.command(
    help="Structure and/or validate one or more files. Argument "
         "is a space-separated list of files or globs."
)
@click.argument(
    'files',
    nargs=-1,
    type=CLICK_FILE,
)
@click.option(
    '--output-filenames',
    '-o',
    type=click.Path(),
    help="Filenames to write each input filename to. "
         "If not specified, auto-names each file by appending"
         "`-structured` before the file extension inside "
         "the current working dir.",
    multiple=True
)
@click.option(
    '--output-dir',
    '-d',
    type=CLICK_DIR,
    help="Directory to dump auto-named files to. Only works if"
         "--output-filenames is not specified."
)
@click.option(
    '--protocol-parameters-dir',
    '-p',
    type=CLICK_DIR,
    help="Directory of a protocol parameters files to use for "
         "auto-structuring. If not specified, BEEP cannot auto-"
         "structure. Use with --automatic."
)
@click.option(
    '--v-range',
    '-v',
    type=(click.FLOAT, click.FLOAT),
    help="Lower, upper bounds for voltage range for structuring. "
         "Overridden by auto-structuring if --automatic."
)
@click.option(
    '--resolution',
    '-r',
    type=click.INT,
    default=1000,
    help="Resolution for interpolation for structuring. Overridden "
         "by auto-structuring if --automatic."
)
@click.option(
    '--nominal-capacity',
    '-n',
    type=click.FLOAT,
    default=1.1,
    help="Nominal capacity to use for structuring. Overridden by "
         "auto-structuring if --automatic."
)
@click.option(
    '--full-fast-charge',
    '-f',
    type=click.FLOAT,
    default=0.8,
    help="Full fast charge threshold to use for structuring. "
         "Overridden by auto-structuring if --automatic."
)
@click.option(
    '--charge-axis',
    '-c',
    type=click.STRING,
    default='charge_capacity',
    help="Axis to use for charge step interpolation. Must be found "
         "inside the loaded dataframe. Can be used with --automatic."
)
@click.option(
    '--discharge-axis',
    '-x',
    type=click.STRING,
    default='voltage',
    help="Axis to use for discharge step interpolation. Must be "
         "found inside the loaded dataframe. Can be used with"
         "--automatic."
)
@click.option(
    '--s3-bucket',
    '-b',
    default=None,
    type=click.STRING,
    help="Expands file paths to include those in the s3 bucket specified. "
         "File paths specify s3 keys. Keys can be globbed/wildcarded. Paths "
         "matching local files will be prioritized over files with identical "
         "paths/globs in s3. Files will be downloaded to CWD."
)
@click.option(
    '--halt-on-error',
    is_flag=True,
    default=False,
    help="Set to halt BEEP if critical structuring "
         "errors are encountered on any file. Otherwise, logs "
         "critical errors to the status json.",
)
@click.option(
    '--automatic',
    is_flag=True,
    default=False,
    help="If --protocol-parameters-path or the BEEP_PARAMETERS_"
         "PATH environment variable is specified, will automatically "
         "determine structuring parameters. Will override all "
         "manually set structuring parameters."
)
@click.option(
    '--validation-only',
    is_flag=True,
    default=False,
    help='Skips structuring, only validates files.'
)
@click.option(
    '--no-raw',
    is_flag=True,
    default=False,
    help="Does not save raw cycler data to disk. Saves disk space, but "
         "prevents files from being partially restructued."
)
@click.option(
    '--s3-use-cache',
    is_flag=True,
    default=False,
    help="Use s3 cache defined with environment variable BEEP_S3_CACHE "
         "instead of downloading files directly to the CWD."
)
@click.pass_context
def structure(
        ctx,
        files,
        output_filenames,
        output_dir,
        protocol_parameters_dir,
        v_range,
        resolution,
        nominal_capacity,
        full_fast_charge,
        charge_axis,
        discharge_axis,
        s3_bucket,
        automatic,
        validation_only,
        no_raw,
        s3_use_cache
):

    # download from s3 first, if needed
    if s3_bucket:
        logger.info(f"Fetching file list from s3 bucket {s3_bucket}...")
        s3_objs = list_s3_objects(s3_bucket)
        logger.info(f"Including {len(s3_objs)} available s3 objects in file match.")
        s3_keys = [o.key for o in s3_objs]

        # local files matching globs are pre-expanded by Click
        s3_keys_matched = []
        local_files = []
        for maybe_glob in files:
            # add direct matches
            if "*" not in maybe_glob:
                if maybe_glob in s3_keys:
                    s3_keys_matched.append(maybe_glob)
                else:
                    local_files.append(maybe_glob)
            else:
                # its a glob, and real local globs will
                # be pre-expanded by click, so the only
                # valid globs will be on s3. All remaining
                # globs are invalid/bad paths
                matching_files = fnmatch.filter(s3_keys, maybe_glob)
                if matching_files:
                    s3_keys_matched.append(matching_files)
                else:
                    local_files.append(maybe_glob)

        logger.info(f"Found {len(s3_keys_matched)} matching files on s3")
        local_files_from_s3 = []
        for s3k in s3_keys_matched:
            s3k_basename = os.path.basename(s3k)
            pardir = S3_CACHE if s3_use_cache else ctx.obj.cwd
            s3k_local_fullname = os.path.join(pardir, s3k_basename)
            logger.info(f"Fetching {s3k} from {s3_bucket}")
            download_s3_object(s3_bucket, s3k, s3k_local_fullname)
            logger.info(f"Fetched s3 file {s3k_basename} to {s3k_local_fullname}")
            local_files_from_s3.append(s3k_local_fullname)
        files = local_files + local_files_from_s3

    files = [os.path.abspath(f) for f in files]

    for file in files:
        if not os.path.exists(file):
            raise FileNotFoundError(f"File '{file}' not found on filesystem!")
    n_files = len(files)

    logger.info(f"Structuring {n_files} files")

    # Output dir overrules output filenames
    if output_dir:
        # Use auto-naming in the output dir
        output_dir = os.path.abspath(output_dir)
        output_files = [
            add_suffix(f, output_dir, STRUCTURED_SUFFIX, modified_ext=".json.gz")
            for f in files
        ]

        if output_filenames:
            logger.warning(
                "Both --output-filenames and --output-dir were specified; "
                "defaulting to --output-dir with auto-naming."
            )
    else:
        if output_filenames:
            output_files = [os.path.abspath(f) for f in output_filenames]
            n_outputs = len(output_files)
            if n_files != n_outputs:
                raise ValueError(
                    f"Number of input files ({n_files}) does not match number "
                    f"of output filenames ({n_outputs})!"
                )
        else:
            # Use auto-naming in the cwd
            output_files = [
                add_suffix(f, ctx.obj.cwd, STRUCTURED_SUFFIX, modified_ext=".json.gz")
                for f in files
            ]

    if protocol_parameters_dir and BEEP_PARAMETERS_DIR:
        logger.warning(
            "Both --protocol-parameters-dir and $BEEP_PARAMETERS_PATH were specified. "
            "Defaulting to path from --protocol-parameters-dir."
        )
        params_dir = protocol_parameters_dir
    elif protocol_parameters_dir and not BEEP_PARAMETERS_DIR:
        params_dir = protocol_parameters_dir
    elif not protocol_parameters_dir and BEEP_PARAMETERS_DIR:
        params_dir = BEEP_PARAMETERS_DIR
    else:
        # neither are defined
        params_dir = None

    if automatic and not params_dir:
        logger.warning(
            "--automatic was passed but no protocol parameters "
            "directory was specified! Either set BEEP_PARAMETERS_DIR "
            "or pass --protocol-parameters-dir to use autostructuring."
        )

    params = {
        "v_range": v_range,
        "resolution": resolution,
        "nominal_capacity": nominal_capacity,
        "full_fast_charge": full_fast_charge,
        "charge_axis": charge_axis,
        "discharge_axis": discharge_axis
    }

    status_json = {}
    log_prefix = "No file"
    for i, f in enumerate(files):
        op_result = {
            "validated": False,
            "validation_schema": None,
            "structured": False,
            "output": None,
            "traceback": None,
            "walltime": None,
            "raw_md5_chksum": None
        }

        t0 = time.time()
        try:
            log_prefix = f"File {i + 1} of {n_files}"
            logger.debug(f"Hashing file '{f}' to MD5")
            op_result["raw_md5_chksum"] = md5sum(f)

            logger.info(f"{log_prefix}: Reading raw file {f} from disk...")
            dp = auto_load(f)
            logger.info(f"{log_prefix}: Validating: {f} according to schema file '{dp.schema}'")
            op_result["validation_schema"] = dp.schema

            is_valid, validation_reason = dp.validate()
            op_result["validated"] = is_valid

            if not is_valid:
                raise BeepValidationError(validation_reason)

            logger.info(f"File {i + 1} of {n_files}: Validated: {f}")

            if not validation_only:
                logger.info(f"{log_prefix}: Structuring: Read from {f}")
                if automatic:
                    dp.autostructure(
                        charge_axis=charge_axis,
                        discharge_axis=discharge_axis,
                        parameters_path=params_dir
                    )
                else:
                    dp.structure(**params)

                output_fname = output_files[i]
                dp.to_json_file(output_fname, omit_raw=no_raw)
                op_result["structured"] = True
                op_result["output"] = output_fname
                logger.info(f"{log_prefix}: Structured: Written to {output_fname}")

        except KeyboardInterrupt:
            logging.critical("Keyboard interrupt caught - exiting...")
            click.Context.exit(1)

        except BaseException:
            tbinfo = sys.exc_info()
            tbfmt = traceback.format_exception(*tbinfo)
            logger.error(f"{log_prefix}: Failed/invalid: ({tbinfo[0].__name__}): {f}")
            op_result["traceback"] = tbfmt

            if ctx.obj.halt_on_error:
                raise

        t1 = time.time()
        op_result["walltime"] = t1 - t0
        status_json[f] = op_result

    # Generate the status report
    succeeded, failed, invalid = [], [], []

    for input_fname, op_result in status_json.items():
        if op_result["validated"] and op_result["structured"]:
            succeeded.append(input_fname)
        elif op_result["validated"] and not op_result["structured"]:
            failed.append(input_fname)
        else:
            invalid.append(input_fname)

    logger.info(f"{'Validation' if validation_only else 'Structuring'} report:")

    logger.info(f"\t{'Structured' if validation_only else 'Succeeded'}: {len(succeeded)}/{n_files}")
    logger.info(f"\tInvalid: {len(invalid)}/{n_files}")
    for inv in invalid:
        logger.info(f"\t\t- {inv}")

    logger.info(f"\t{'Validated, not structured' if validation_only else 'Failed'}: {len(failed)}/{n_files}")
    for fail in failed:
        logger.info(f"\t\t- {fail}")

    status_json = add_metadata_to_status_json(status_json, ctx.obj.run_id, ctx.obj.tags)

    osj = ctx.obj.output_status_json
    if osj:
        dumpfn(status_json, osj)
        logger.info(f"Wrote status json file to {osj}")


@cli.command(
    help="Featurize one or more files. Argument "
         "is a space-separated list of files or globs. The same "
         "features are applied to each file. Naming of output"
         "files is done automatically, but the output directory "
         "can be specified."
)
@click.argument(
    'files',
    nargs=-1,
    type=CLICK_FILE,
)
@click.option(
    '--output-dir',
    '-d',
    type=CLICK_DIR,
    help="Directory to dump auto-named files to."
)
@click.option(
    '--featurize-with',
    "-f",
    default=["all"],
    multiple=True,
    type=click.STRING,
    help="Specify a featurizer to apply by class name, e.g. "
         "HPPCResistanceVoltageFeatures. To apply more than one "
         "featurizer, use multiple -f <FEATURIZER> commands. To apply"
         "all core BEEP featurizers, pass the value 'all'. Note if 'all'"
         "is passed other -f featurizers will be ignored. All "
         "feautrizers are attempted to apply with default hyperparameters; "
         "to specify your own hyperparameters, use --featurize-with-hyperparams."
         "Classes from installed modules not in core BEEP can be "
         "specified with the class name in absolute import format, "
         "e.g., my_package.my_module.MyClass."
)
@click.option(
    "--featurize-with-hyperparams",
    "-h",
    multiple=True,
    help="Specify a featurizer to apply by class name with your own hyperparameters."
         "(such as parameter directories or specific values for hyperparameters"
         "for this featurizer), pass a dictionary in the format:"
         "'{\"FEATURIZER_NAME\": {\"HYPERPARAM1\": \"VALUE1\"...}}' including the "
         "single quotes around the outside and double quotes for internal strings."
         "Custom hyperparameters will be merged with default hyperparameters if the "
         "hyperparameter dictionary is underspecified."
)

@click.pass_context
def featurize(
        ctx,
        files,
        output_dir,
        featurize_with,
        featurize_with_hyperparams,
):
    files = [os.path.abspath(f) for f in files]
    n_files = len(files)
    output_dir = os.path.abspath(output_dir) if output_dir else ctx.obj.cwd

    logger.info(f"Featurizing {n_files} files")

    core_fclasses = [
        HPPCResistanceVoltageFeatures,
        DeltaQFastCharge,
        TrajectoryFastCharge,
        CycleSummaryStats,
        DiagnosticProperties,
        DiagnosticSummaryStats,
    ]
    native_fclasses = core_fclasses + [IntracellCycles, IntracellFeatures]

    core_fclasses_map = {fclass.__name__: fclass for fclass in core_fclasses}
    native_fclasses_map = {fclass.__name__: fclass for fclass in native_fclasses}

    # Create canonical featurizer list if "all" is selected
    if "all" in featurize_with:
        featurize_with = list(core_fclasses_map.keys())

    # Feature class names along with hyperparameters
    # These are all default
    fclass_names_w_params = [(fclass_name, None) for fclass_name in featurize_with]

    # Add featurizers with custom parameters to list of featurizers to apply
    for fstr in featurize_with_hyperparams:
        fdict = ast.literal_eval(fstr)
        if not isinstance(fdict, dict):
            raise TypeError(f"Could not parse input featurizer with parameters string {fdict}")
        if len(fdict) != 1:
            raise ValueError(f"Featurizer must be specified as sole root key of hyperparam dictionary: {fdict}")
        fclass_name_w_params = [(k, v) for k, v in fdict.items()][0]
        fclass_names_w_params.append(fclass_name_w_params)

    # Determine actual classes to apply by joining with external modules
    fclass_tuples = []
    for fclass_name, fclass_params in fclass_names_w_params:
        if fclass_name in native_fclasses_map:
            fclass = native_fclasses_map[fclass_name]
        else:
            # it is assumed it will be an external module
            if "." not in fclass_name:
                logging.critical(
                    f"'{fclass_name}' not recognized as BEEP native featurizer "
                    f"or importable module."
                )
                click.Context.exit(1)

            modname, _, clsname = fclass_name.rpartition('.')
            mod = importlib.import_module(modname)
            cls = getattr(mod, clsname)

            if not issubclass(cls, BEEPFeaturizer):
                logging.critical(f"Class {cls.__name__} is not a subclass of BeepFeatures.")
                click.Context.exit(1)
            fclass = cls

        # check parameter arguments and update with full hyperparameter specifications
        hps = fclass.DEFAULT_HYPERPARAMETERS
        if fclass_params is not None:
            hps.update(fclass_params)
        fclass_tuples.append((fclass, hps))


    logger.info(f"Applying {len(fclass_tuples)} featurizers to each of {n_files} files")

    # ragged featurizers apply is ok

    status_json = {}
    i = 0
    for file in files:
        log_prefix = f"File {i + 1} of {n_files}"

        t0_file = time.time()
        op_result = {
            "walltime": None,
            "processed_md5_chksum": md5sum(file)

        }

        logger.debug(f"{log_prefix}: Loading processed run '{file}'.")
        structured_datapath = auto_load_processed(file)
        logger.debug(f"{log_prefix}: Loaded processed run '{file}' into memory.")

        for fclass, f_hyperparams in fclass_tuples:
            op_subresult = {
                "output": None,
                "valid": False,
                "featurized": False,
                "walltime": None,
                "traceback": None,
                "op_md5_chksum": None
            }
            fclass_name = fclass.__name__

            t0 = time.time()
            try:

                f = fclass(
                    structured_datapath=structured_datapath,
                    hyperparameters=f_hyperparams
                )

                is_valid, reason = f.validate()

                if is_valid:
                    op_subresult["valid"] = True
                    logger.info(f"{log_prefix}: Featurizer {fclass_name} valid with params {f_hyperparams} for '{file}'")
                else:
                    raise BEEPFeaturizationError(reason)

                f.create_features()
                op_subresult["featurized"] = True
                logger.info(
                    f"{log_prefix}: Featurizer {fclass_name} applied with params {f_hyperparams} for '{file}'")

                output_filename = f"{fclass_name}-{os.path.basename(file)}"
                output_path = os.path.join(output_dir, output_filename)
                dumpfn(f, output_path)
                logger.info(
                    f"{log_prefix}: Featurizer {fclass_name} features for '{file}' written to '{output_path}'")
                op_subresult["output"] = output_path
                op_subresult["op_md5_chksum"] = md5sum(output_path)

            except KeyboardInterrupt:
                logger.critical("Keyboard interrupt caught - exiting...")
                click.Context.exit(1)

            except BaseException:
                tbinfo = sys.exc_info()
                tbfmt = traceback.format_exception(*tbinfo)
                logger.error(
                    f"{log_prefix}: Failed/invalid: ({tbinfo[0].__name__}): {fclass.__name__}")
                op_subresult["traceback"] = tbfmt

                if ctx.obj.halt_on_error:
                    raise

            t1 = time.time()
            op_subresult["walltime"] = t1 - t0
            op_result[fclass_name] = op_subresult

        t1_file = time.time()
        op_result["walltime"] = t1_file - t0_file
        status_json[file] = op_result
        i += 1

    # Generate a summary output

    logger.info("Featurization report:")

    all_succeeded, some_succeeded, none_succeeded = [], [], []
    for file, data in status_json.items():
        feats_succeeded = []
        for fname, fdata in data.items():
            if fname not in ["walltime", "processed_md5_chksum"]:
                feats_succeeded.append(fdata["featurized"])

        n_success = sum(feats_succeeded)
        if n_success == len(fclass_tuples):
            all_succeeded.append((file, n_success))
        elif n_success == 0:
            none_succeeded.append((file, n_success))
        else:
            some_succeeded.append((file, n_success))

    logger.info(f"\tAll {len(fclass_tuples)} featurizers succeeded: {len(all_succeeded)}/{n_files}")
    if len(all_succeeded) > 0:
        for filename, _ in all_succeeded:
            logger.info(f"\t\t- {filename}")

    if len(fclass_tuples) > 1:
        logger.info(f"\tSome featurizers succeeded: {len(some_succeeded)}/{n_files}")
        if len(some_succeeded) > 0:
            for filename, n_success in some_succeeded:
                logger.info(f"\t\t- {filename}: {n_success}/{len(fclass_tuples)}")

    logger.info(f"\tNo featurizers succeeded or file failed: {len(none_succeeded)}/{n_files}")
    if len(none_succeeded) > 0:
        for filename, _ in none_succeeded:
            logger.info(f"\t\t- {filename}")

    status_json = add_metadata_to_status_json(status_json, ctx.obj.run_id, ctx.obj.tags)
    osj = ctx.obj.output_status_json
    if osj:
        dumpfn(status_json, osj)
        logger.info(f"Wrote status json file to {osj}")


@cli.command(
    help="Train a machine learning model"
)
@click.argument(
    'files',
    nargs=-1,
    type=CLICK_FILE,
)
@click.pass_context
def train(ctx, files):
    pass


@cli.command(
    help="Predict using a pre-trained model"
)
@click.argument(
    'feature_files',
    nargs=-1,
    type=CLICK_FILE,
)
@
@click.pass_context
def predict(ctx, files):
    pass



