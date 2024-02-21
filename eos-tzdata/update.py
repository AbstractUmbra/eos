import argparse
import io
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import typing

import requests

IANA_LATEST_LOCATION = 'https://www.iana.org/time-zones/repository/tzdata-latest.tar.gz'
SOURCE = 'https://data.iana.org/time-zones/releases'
WORKING_DIR = pathlib.Path('tmp')
REPO_ROOT = pathlib.Path(__file__).parent
PKG_BASE = REPO_ROOT / 'src'
DATA_TEMPLATE_FILE = """// This file is automatically generated
// Please do not touch it.
// The data in this file corresponds to the IANA database version {version}

use crate::ZoneEntry;

pub const MAPPINGS: [ZoneEntry; {length}] = [
{data}
];
"""
CARGO_TOML_VERSION = re.compile(r'^version = \"1\.(?P<version>\d{4,}\.\d+)\"', re.MULTILINE)


def download_tzdb_tarballs(
    version: str, base_url: str = SOURCE, working_dir: pathlib.Path = WORKING_DIR
) -> typing.List[pathlib.Path]:
    """Download the tzdata and tzcode tarballs."""
    tzdata_file = f'tzdata{version}.tar.gz'
    tzcode_file = f'tzcode{version}.tar.gz'

    target_dir = working_dir / version / 'download'
    # mkdir -p target_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    download_locations = []
    for filename in [tzdata_file, tzcode_file]:
        download_location = target_dir / filename
        download_locations.append(download_location)

        if download_location.exists():
            print(f'info: file {download_location} already exists, skipping')
            continue

        url = f'{base_url}/{filename}'
        print(f'info: downloading {filename} from {url}', filename, url)

        r = requests.get(url)
        with open(download_location, 'wb') as f:
            f.write(r.content)

    return download_locations


def retrieve_local_tarballs(
    version: str, source_dir: pathlib.Path, working_dir: pathlib.Path = WORKING_DIR
) -> typing.List[pathlib.Path]:
    """Retrieve the tzdata and tzcode tarballs from a folder.

    This is useful when building against a local, patched version of tzdb.
    """
    tzdata_file = f'tzdata{version}.tar.gz'
    tzcode_file = f'tzcode{version}.tar.gz'

    target_dir = working_dir / version / 'download'

    # mkdir -p target_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    dest_locations = []

    for filename in [tzdata_file, tzcode_file]:
        source_location = source_dir / filename
        dest_location = target_dir / filename

        if dest_location.exists():
            print(f'info: file {dest_location} exists, overwriting')

        shutil.copy(source_location, dest_location)

        dest_locations.append(dest_location)

    return dest_locations


def unpack_tzdb_tarballs(download_locations: typing.List[pathlib.Path]) -> pathlib.Path:
    assert len(download_locations) == 2
    assert download_locations[0].parent == download_locations[1].parent
    base_dir = download_locations[0].parent.parent
    target_dir = base_dir / 'tzdb'

    # Remove the directory and re-create it if it does not exist
    if target_dir.exists():
        shutil.rmtree(target_dir)

    target_dir.mkdir()

    for tarball in download_locations:
        print(f'info: unpacking {tarball} to {target_dir}')
        with tarfile.open(tarball.absolute(), mode='r:gz') as fp:
            fp.extractall(target_dir)

    return target_dir


def load_zonefiles(
    base_dir: pathlib.Path,
) -> typing.Tuple[typing.List[str], pathlib.Path]:
    target_dir = base_dir.parent / 'zoneinfo'
    if target_dir.exists():
        shutil.rmtree(target_dir)

    with tempfile.TemporaryDirectory() as td:
        td_path = pathlib.Path(td)

        # First run the makefile, which does all kinds of other random stuff
        subprocess.run(
            ['make', f'DESTDIR={td}', 'POSIXRULES=-', 'ZFLAGS=-b slim', 'install'],
            cwd=base_dir,
            check=True,
        )

        proc = subprocess.run(['make', 'zonenames'], cwd=base_dir, stdout=subprocess.PIPE, check=True)
        zonenames = list(map(str.strip, proc.stdout.decode('utf-8').split('\n')))

        # Move the zoneinfo files into the target directory
        src_dir = td_path / 'usr' / 'share' / 'zoneinfo'
        shutil.move(os.fspath(src_dir), os.fspath(target_dir))

    return zonenames, target_dir


def is_already_latest_version(version: str) -> bool:
    """Returns ``True`` if the version is already the latest version"""
    try:
        with open('VERSION', 'r') as fp:
            other = fp.read().strip()
    except OSError:
        return False
    else:
        lhs = tuple(map(int, version.split('.')))
        rhs = tuple(map(int, other.split('.')))
        return lhs <= rhs


def python_bytes_to_rust(b: bytes) -> str:
    # turn b'1\x9f10' to the Rust version with double quotes
    inner = ''.join(chr(a) if 0x7F >= a >= 0x20 and a not in (0x22, 0x5C) else f'\\x{a:02x}' for a in b)
    return f'b"{inner}"'


def convert_to_rust_struct(name: str, tzif: bytes) -> str:
    # name is always ASCII without " as far as I'm aware
    return f'    ZoneEntry {{ zone: "{name}", data: {python_bytes_to_rust(tzif)} }},\n'


def update_package(version: str, zonenames: typing.List[str], zoneinfo_dir: pathlib.Path):
    """Creates the tzdata package."""
    package_version = translate_version(version)
    if is_already_latest_version(package_version):
        print(f'info: {version} is already the newest TZDB version, no work to do.')
        return

    # Sort the zone names by lexicographical position
    zonenames = sorted(zonenames)

    # fmt: off
    # Generate a compile mapping of zone name -> byte data
    zone_to_bytes = {
        name: open(zoneinfo_dir / name, 'rb').read()
        for name in zonenames
        if (zoneinfo_dir / name).is_file()
    }

    # Convert the mapping into a Rust struct literal
    data = [
        convert_to_rust_struct(name, tzif)
        for name, tzif in zone_to_bytes.items()
    ]
    # fmt: on

    # Create the actual src/data.rs
    with open(PKG_BASE / 'data.rs', 'w', encoding='utf-8', newline='\n') as fp:
        length = len(data)
        fp.write(DATA_TEMPLATE_FILE.format(length=length, version=version, data=''.join(data)))

    # Write the actual VERSION
    with open(REPO_ROOT / 'VERSION', 'w') as f:
        f.write(package_version)

    # Update the Cargo.toml version
    with open(REPO_ROOT / 'Cargo.toml', 'r+', encoding='utf-8', newline='\n') as fp:
        contents = fp.read()
        updated = CARGO_TOML_VERSION.sub(f'version = "1.{package_version}"', contents, count=1)
        fp.seek(0)
        fp.write(updated)
        fp.truncate()


def find_latest_version() -> str:
    r = requests.get(IANA_LATEST_LOCATION)
    fobj = io.BytesIO(r.content)
    with tarfile.open(fileobj=fobj, mode='r:gz') as tf:
        vfile = tf.extractfile('version')

        assert vfile is not None, 'version file is not a regular file'
        version = vfile.read().decode('utf-8').strip()

    assert re.match(r'\d{4}[a-z]$', version), version

    target_dir = WORKING_DIR / version / 'download'
    target_dir.mkdir(parents=True, exist_ok=True)

    fobj.seek(0)
    with open(target_dir / f'tzdata{version}.tar.gz', 'wb') as f:
        f.write(fobj.read())

    return version


def translate_version(iana_version: str) -> str:
    """Translates from an IANA version to a PEP 440 version string.

    E.g. 2020a -> 2020.1
    """

    if len(iana_version) < 5 or not iana_version[0:4].isdigit() or not iana_version[4:].isalpha():
        raise ValueError(
            'IANA version string must be of the format YYYYx where YYYY represents the '
            f'year and x is in [a-z], found: {iana_version}'
        )

    version_year = iana_version[0:4]
    patch_letters = iana_version[4:]

    # From tz-link.html:
    #
    # Since 1996, each version has been a four-digit year followed by
    # lower-case letter (a through z, then za through zz, then zza through zzz,
    # and so on).
    if len(patch_letters) > 1 and not all(c == 'z' for c in patch_letters[0:-1]):
        raise ValueError(
            f'Invalid IANA version number (only the last character may be a letter other than z), found: {iana_version}'
        )

    final_patch_number = ord(patch_letters[-1]) - ord('a') + 1
    patch_number = (26 * (len(patch_letters) - 1)) + final_patch_number

    return f'{version_year}.{patch_number:d}'


def main():
    parser = argparse.ArgumentParser(usage='Updates the IANA data')
    parser.add_argument('--version', '-v', default=None, help='The version of the tzdata file to download')
    parser.add_argument(
        '--source-dir',
        '-s',
        default=None,
        help='A local source directory containing tarballs (must be used together with --version)',
        type=pathlib.Path,
    )
    args = parser.parse_args()

    if args.source_dir is not None:
        if args.version is None:
            parser.error('--source-dir specified without --version.\nIf using --source-dir, --version must also be used.')
        download_locations = retrieve_local_tarballs(args.version, args.source_dir)
    else:
        if args.version is None:
            args.version = find_latest_version()

        download_locations = download_tzdb_tarballs(args.version)

    tzdb_location = unpack_tzdb_tarballs(download_locations)
    zonenames, zonefile_path = load_zonefiles(tzdb_location)
    update_package(args.version, zonenames, zonefile_path)


if __name__ == '__main__':
    main()
