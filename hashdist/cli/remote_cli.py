import sys
import os
import subprocess
import shutil
from os.path import join as pjoin, exists as pexists
from textwrap import dedent
import json

from ..formats.config import (
    DEFAULT_STORE_DIR,
    DEFAULT_CONFIG_DIRS,
    DEFAULT_CONFIG_FILENAME_REPR,
    DEFAULT_CONFIG_FILENAME,
    get_config_example_filename
)
from .main import register_subcommand


@register_subcommand
class Remote(object):

    """
    Manage remote build store and source cache.

    Currently, only common cloud-based storage is supported
    (https://github.com/netheosgithub/pcs_api).


    Example:: **First**, create a dropbox app for your remote at
    https://www.dropbox.com/developers/apps. Next, add the remote:

        $ hit remote add --pcs="dropbox" --app-name="hd_osx" \\
          --app-id=x --app-secret=y

    """
    command = 'remote'

    @staticmethod
    def setup(ap):
        ap.add_argument('subcommand', choices=['add', 'show'])
        ap.add_argument('name', default="primary", nargs="?",
                        help="Name of remote")
        ap.add_argument('--pcs', default="dropbox",
                        help='Use personal cloud service')
        ap.add_argument('--app-name', default="hashdist_PLATFORM",
                        help='Name of the app on the cloud service')
        ap.add_argument('--app-id', default=None,
                        help='ID of the app on cloud service')
        ap.add_argument('--app-secret', default=None,
                        help='Secret for the app on cloud service')

    @staticmethod
    def run(ctx, args):
        import pcs_api.credentials as pcs_cred
        from pcs_cred.app_info_file_repo import AppInfoFileRepository
        from pcs_cred.user_creds_file_repo import UserCredentialsFileRepository
        from pcs_api.credentials.user_credentials import UserCredentials
        from pcs_api.oauth.oauth2_bootstrap import OAuth2BootStrapper
        from pcs_api.storage import StorageFacade
        # Required for registering providers :
        from pcs_api.providers import (dropbox,
                                       googledrive)
        #
        if args.subcommand == 'add':
            ctx.logger.info("Attempting to add remote")
            remote_path = pjoin(DEFAULT_STORE_DIR, "remotes", args.name)
            try:
                if not os.path.exists(remote_path):
                    os.makedirs(remote_path)
            except:
                ctx.logger.critical("Could not create:" + repr(remote_path))
                exit(1)
            if None in [args.app_id, args.app_secret]:
                ctx.logger.critical("Supply both --app-id and --app-secret")
                exit(1)
            app_info_data = '{pcs}.{app_name} = {{ "appId": "{app_id}", \
            "appSecret": "{app_secret}", \
            "scope": ["sandbox"] }}'.format(**args.__dict__)
            app_info_path = pjoin(remote_path, "app_info_data.txt")
            user_credentials_path = pjoin(remote_path,
                                          "user_credentials_data.txt")
            f = open(app_info_path, "w")
            f.write(app_info_data)
            f.close()
            apps_repo = AppInfoFileRepository(app_info_path)
            user_credentials_repo = UserCredentialsFileRepository(
                user_credentials_path)
            storage = StorageFacade.for_provider(args.pcs) \
                .app_info_repository(apps_repo, args.app_name) \
                .user_credentials_repository(user_credentials_repo) \
                .for_bootstrap() \
                .build()
            bootstrapper = OAuth2BootStrapper(storage)
            bootstrapper.do_code_workflow()
        elif args.subcommand == 'show':
            for remote_name in os.listdir(pjoin(DEFAULT_STORE_DIR, "remotes")):
                sys.stdout.write(remote_name + "\n")
                if args.verbose:
                    import pprint
                    pp = pprint.PrettyPrinter(indent=4)
                    sys.stdout.write('=' * len(remote_name) + '\n')
                    with open(pjoin(DEFAULT_STORE_DIR,
                                    "remotes",
                                    remote_name,
                                    "app_info_data.txt"), "r") as f:
                        for line in f.readlines():
                            if not line.strip().startswith("#"):
                                app_name, app_dict = line.split("=")
                                sys.stdout.write(app_name + " = \n")
                                pp.pprint(json.loads(app_dict))
                        sys.stdout.write("\n")
                    with open(pjoin(DEFAULT_STORE_DIR,
                                    "remotes",
                                    remote_name,
                                    "user_credentials_data.txt"), "r") as f:
                        for line in f.readlines():
                            if not line.strip().startswith("#"):
                                app_user, app_cred_dict = line.split("=")
                                sys.stdout.write(app_user + " = \n")
                                pp.pprint(json.loads(app_cred_dict))
        else:
            raise AssertionError()


@register_subcommand
class Push(object):

    """
    Push artifacts to remote build store

    Example::

        $ hit push

    """
    command = 'push'

    @staticmethod
    def setup(ap):
        ap.add_argument('--dry-run', action='store_true',
                        help='Show what would happen')
        ap.add_argument('--force', action='store_true',
                        help='Force push of all packages')
        ap.add_argument('--objects', default="build_and_source",
                        help="Push 'build','source', or 'build_and_source'")
        ap.add_argument('name', default="primary", nargs="?",
                        help="Name of remote")

    @staticmethod
    def run(ctx, args):
        import hashlib
        # Required for providers registration :
        from pcs_api.providers import (dropbox,
                                       googledrive)
        #
        import pcs_api.credentials as pcs_cred
        from pcs_cred.app_info_file_repo import AppInfoFileRepository
        from pcs_cred.user_creds_file_repo import UserCredentialsFileRepository
        from pcs_cred.user_credentials import UserCredentials
        from pcs_api.storage import StorageFacade
        from pcs_api.bytes_io import (MemoryByteSource, MemoryByteSink,
                                      FileByteSource, FileByteSink,
                                      StdoutProgressListener)
        from pcs_api.models import (CPath,
                                    CFolder,
                                    CBlob,
                                    CUploadRequest,
                                    CDownloadRequest)
        # set up store and change to the artifact root  dir
        from ..core import BuildStore, SourceCache
        if not args.dry_run:
            ctx.logger.info("Setting up cloud storage app")
            remote_path = pjoin(DEFAULT_STORE_DIR, "remotes", arg.name)
            app_info_path = pjoin(remote_path, "app_info_data.txt")
            user_credentials_path = pjoin(remote_path,
                                          "user_credentials_data.txt")
            if not os.path.exists(app_info_path):
                msg = 'No remote application information: ' \
                    + repr(app_info_path)
                ctx.logger.critical(msg)
                msg = "Run 'hit remote add ...'"
                ctx.logger.critical(msg)
                exit(1)
            apps_repo = AppInfoFileRepository(app_info_path)
            if not os.path.exists(user_credentials_path):
                msg = 'No user credentials found: ' \
                    + repr(user_credentials_path)
                ctx.logger.critical(msg)
                msg = "Run 'hit remote add ...'"
                ctx.logger.critical(msg)
                exit(1)
            user_credentials_repo = UserCredentialsFileRepository(
                user_credentials_path)
            provider_name = apps_repo._app_info.keys()[0].split(".")[0]
            app_info = apps_repo.get(provider_name)
            user_info = user_credentials_repo.get(app_info)
            storage = StorageFacade.for_provider(provider_name) \
                .app_info_repository(apps_repo, app_info.app_name) \
                .user_credentials_repository(user_credentials_repo,
                                             user_info.user_id) \
                .build()
            msg = "Cloud storage user_id = " + repr(storage.get_user_id())
            ctx.logger.info(msg)
            msg = "Cloud storage quota = " + repr(storage.get_quota())
            ctx.logger.info()
            ctx.logger.info("Cloud storage is  ready")
            ctx.logger.info("Getting remote manifest")

        if args.objects in ['build', 'build_and_source']:
            store = BuildStore.create_from_config(ctx.get_config(), ctx.logger)
            os.chdir(store.artifact_root)
            # try loading the local copy of the remote manifest
            try:
                with open(pjoin("..",
                                "build_manifest.json"), "r") as manifest_file:
                    local_manifest = json.loads(manifest_file.read())
            except:
                ctx.logger.warn("Using an empty local manifest because
                                build_manifest.json could not be read")
                local_manifest = {}
            if args.dry_run:
                ctx.logger.info("Comparing build store to last local copy of
                                remote manifest")
                skipping = ''
                pushing = ''
                for package in os.listdir(store.artifact_root):
                    for artifact in os.listdir(pjoin(store.artifact_root,
                                                     package)):
                        if (package in local_manifest and
                                artifact in local_manifest[package]):
                            skipping += package + "/" + \
                                artifact + " Skipping\n"
                        else:
                            pushing += package + "/" + artifact + " Pushing\n"
                sys.stdout.write(skipping)
                sys.stdout.write("Use --force to push all artifacts\n")
                sys.stdout.write(pushing)
            else:
                try:
                    remote_manifest_string = MemoryByteSink()
                    fpath = CPath('/bld/')
                    bpath = fpath.add("build_manifest.json")
                    download_request = CDownloadRequest(bpath,
                                                        remote_manifest_string)
                    download_request.progress_listener(
                        StdoutProgressListener())
                    storage.download(download_request)
                    manifest = json.loads(
                        str(remote_manifest_string.get_bytes()))
                except:
                    msg = "Failed to get remote manifest; \
                    ALL PACKAGES WILL BE PUSHED"
                    ctx.logger.warn(msg)
                    manifest = {}
                ctx.logger.info("Writing local copy of remote  manifest")
                with open(pjoin("..", "build_manifest.json"), "w") as f:
                    f.write(json.dumps(manifest))
                ctx.logger.info("Calculating which packages to push")
                push_manifest = {}
                for package in os.listdir(store.artifact_root):
                    if package not in manifest:
                        manifest[package] = {}
                    for artifact in os.listdir(pjoin(store.artifact_root,
                                                     package)):
                        if (artifact in manifest[package] and
                                not args.force):
                            msg = package + "/" + artifact + \
                                " already on remote"
                            ctx.logger.info(msg)
                            # could compare the hashes of the binary package
                        else:
                            if package not in push_manifest:
                                push_manifest[package] = set()
                            push_manifest[package].add(artifact)
                ctx.logger.info("Artifacts to push" + repr(push_manifest))
                for package, artifacts in push_manifest.iteritems():
                    for artifact in artifacts:
                        artifact_path = pjoin(package, artifact)
                        artifact_tgz = artifact + ".tar.gz"
                        artifact_tgz_path = pjoin(package, artifact_tgz)
                        ctx.logger.info("Packing and hashing " +
                                        repr(artifact_tgz_path))
                        subprocess.check_call(["tar", "czf",
                                               artifact_tgz_path,
                                               artifact_path])
                        with open(artifact_tgz_path, "rb") as f:
                            sha1 = hashlib.sha1()
                            sha1.update(f.read())
                            manifest[package][artifact] = sha1.hexdigest()
                        msg = "Pushing " + repr(artifact_tgz_path)+"\n"
                        ctx.logger.info()
                        fpath = CPath('/bld/' + package)
                        storage.create_folder(fpath)
                        bpath = fpath.add(artifact_tgz)
                        upload_request = CUploadRequest(
                            bpath, FileByteSource(artifact_tgz_path))
                        upload_request.progress_listener(
                            StdoutProgressListener())
                        storage.upload(upload_request)
                        ctx.logger.info("Cleaning up and syncing manifest")
                        os.remove(artifact_tgz_path)
                        new_manifest_string = json.dumps(manifest)
                        new_manifest_bytes = bytes(new_manifest_string)
                        manifest_byte_source = MemoryByteSource(
                            new_manifest_bytes)
                        fpath = CPath('/bld/')
                        bpath = fpath.add("build_manifest.json")
                        upload_request = CUploadRequest(
                            bpath,
                            manifest_byte_source).content_type('text/plain')
                        upload_request.progress_listener(
                            StdoutProgressListener())
                        storage.upload(upload_request)
                        with open(pjoin("..",
                                        "build_manifest.json"), "w") as f:
                            f.write(new_manifest_string)
        if args.objects in ['source', 'build_and_source']:
            cache = SourceCache.create_from_config(ctx.get_config(),
                                                   ctx.logger)
            os.chdir(cache.cache_path)
            # try loading the local copy of the remote manifest
            try:
                with open(pjoin("..",
                                "source_manifest.json"), "r") as manifest_file:
                    local_manifest = json.loads(manifest_file.read())
            except:
                msg = "Using an empty local manifest because \
                source_manifest.json could not be read"
                ctx.logger.warn(msg)
                local_manifest = {}
            if args.dry_run:
                msg = "Comparing source to last local copy of remote manifest"
                ctx.logger.info(msg)
                skipping = ''
                pushing = ''
                for subdir in [pjoin('packs', pack_type) for
                               pack_type in ['tar.bz2', 'tar.gz', 'zip']]:
                    for source_pack in os.listdir(pjoin(cache.cache_path,
                                                        subdir)):
                        if (subdir in local_manifest and
                                source_pack in local_manifest[subdir]):
                            skipping += subdir + "/" + \
                                source_pack + " Skipping\n"
                        else:
                            pushing += subdir + "/" + \
                                source_pack + " Pushing\n"
                sys.stdout.write(skipping)
                sys.stdout.write("Use --force to push skipped source packs\n")
                sys.stdout.write(pushing)
            else:
                try:
                    remote_manifest_string = MemoryByteSink()
                    fpath = CPath('/src/')
                    bpath = fpath.add("source_manifest.json")
                    download_request = CDownloadRequest(bpath,
                                                        remote_manifest_string)
                    download_request.progress_listener(
                        StdoutProgressListener())
                    storage.download(download_request)
                    manifest = json.loads(
                        str(remote_manifest_string.get_bytes()))
                except:
                    msg = "Failed to get remote manifest; \
                    all packages will be pushed"
                    ctx.logger.warn(msg)
                    manifest = {}
                ctx.logger.info("Writing local copy of remote  manifest")
                with open(pjoin("..", "source_manifest.json"), "w") as f:
                    f.write(json.dumps(manifest))
                ctx.logger.info("Calculating which packages to push")
                push_manifest = {}
                for subdir in [pjoin('packs', pack_type)
                               for pack_type in ['tar.bz2', 'tar.gz', 'zip']]:
                    if subdir not in manifest:
                        manifest[subdir] = []
                    for source_pack in os.listdir(pjoin(cache.cache_path,
                                                        subdir)):
                        if source_pack in manifest[subdir] and not args.force:
                            msg = subdir + "/" + source_pack + \
                                " already on remote"
                            ctx.logger.info(msg)
                        else:
                            if subdir not in push_manifest:
                                push_manifest[subdir] = set()
                            push_manifest[subdir].add(source_pack)
                ctx.logger.info("Source packs to push" + repr(push_manifest))
                for subdir, source_packs in push_manifest.iteritems():
                    for source_pack in source_packs:
                        manifest[subdir].append(source_pack)
                        source_pack_path = pjoin(subdir, source_pack)
                        msg = "Pushing " + repr(source_pack_path)+"\n"
                        sys.stdout.write(msg)
                        fpath = CPath('/src/' + subdir)
                        storage.create_folder(fpath)
                        bpath = fpath.add(source_pack)
                        upload_request = CUploadRequest(
                            bpath,
                            FileByteSource(source_pack_path))
                        upload_request.progress_listener(
                            StdoutProgressListener())
                        storage.upload(upload_request)
                        ctx.logger.info("Syncing manifest")
                        new_manifest_string = json.dumps(manifest)
                        new_manifest_bytes = bytes(new_manifest_string)
                        manifest_byte_source = MemoryByteSource(
                            new_manifest_bytes)
                        fpath = CPath('/src')
                        bpath = fpath.add("source_manifest.json")
                        upload_request = CUploadRequest(
                            bpath,
                            manifest_byte_source).content_type('text/plain')
                        upload_request.progress_listener(
                            StdoutProgressListener())
                        storage.upload(upload_request)
                        with open(pjoin("..",
                                        "source_manifest.json"), "w") as f:
                            f.write(new_manifest_string)
