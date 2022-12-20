import argparse
import os
import platform
import shutil
import subprocess
import sys
from typing import List

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
DIST_DIR = os.path.join(THIS_DIR, "dist")

PX4_DIR = os.path.join(THIS_DIR, "build", "PX4-Autopilot")

# warning, v1.10.2 does not appear to build anymore
with open(os.path.join(THIS_DIR, ".px4-version"), "r") as fp:
    PX4_VERSION = fp.read().strip()

if PX4_VERSION < "v1.13.0":
    PYMAVLINK_DIR = os.path.join(THIS_DIR, "build", "pymavlink")
else:
    PYMAVLINK_DIR = os.path.join(
        PX4_DIR,
        "src",
        "modules",
        "mavlink",
        "mavlink",
        "pymavlink",
    )


def check_sudo() -> None:
    # skip these checks on Windows
    if sys.platform == "win32":
        return

    # Check if Docker requires sudo
    result = subprocess.run(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if result.returncode == 0:
        # either we have permission to run docker as non-root
        # or we have sudo
        return

    # re run ourselves with sudo
    print("Needing sudo privileges to run docker, re-launching")

    try:
        sys.exit(
            subprocess.run(["sudo", sys.executable, __file__] + sys.argv[1:]).returncode
        )
    except PermissionError:
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(1)


def print2(msg: str) -> None:
    print(f"--- {msg}", flush=True)


def clean_directory(directory: str, file_endings: List[str]) -> None:
    # cancel if the directory is not already there
    if not os.path.isdir(directory):
        return

    for filename in os.listdir(directory):
        if any(filename.endswith(e) for e in file_endings):
            os.remove(os.path.join(directory, filename))


def clone_pymavlink() -> None:
    if os.path.isdir(PYMAVLINK_DIR):
        # update the checkout if we already have it
        print2("Updating pymavlink")
        subprocess.check_call(["git", "pull"], cwd=PYMAVLINK_DIR)

    else:
        # clone fresh
        print2("Cloning pymavlink")
        subprocess.check_call(
            ["git", "clone", "https://github.com/ardupilot/pymavlink", PYMAVLINK_DIR]
        )


def clone_px4() -> None:
    if os.path.isdir(PX4_DIR):
        # first, figure out what version we have locally
        local_version = next(
            l.split("/")[-1]
            for l in subprocess.check_output(
                ["git", "remote", "show", "origin", "-n"], cwd=PX4_DIR
            )
            .decode()
            .splitlines()
            if l.strip().startswith("refs")
        )
        if local_version != PX4_VERSION:
            print(f"Existing PX4 checkout is {local_version}, re-cloning")
            shutil.rmtree(PX4_DIR)
            clone_px4()

        # reset checkout if we already have it
        # this will fail on PX4 version changes
        print2("Resetting PX4 checkout")
        subprocess.check_call(["git", "fetch", "origin"], cwd=PX4_DIR)
        subprocess.check_call(["git", "checkout", PX4_VERSION], cwd=PX4_DIR)
        subprocess.check_call(["git", "reset", "--hard", PX4_VERSION], cwd=PX4_DIR)
        subprocess.check_call(["git", "pull", "--recurse-submodules"], cwd=PX4_DIR)
    else:
        # clone fresh
        print2("Cloning PX4")
        subprocess.check_call(
            [
                "git",
                "clone",
                "https://github.com/PX4/PX4-Autopilot",
                PX4_DIR,
                "--depth",
                "1",
                "--branch",
                PX4_VERSION,
                "--recurse-submodules",
            ]
        )

    print2("Applying PX4 patch")
    subprocess.check_call(
        [
            "git",
            "apply",
            "--ignore-space-change",
            "--ignore-whitespace",
            os.path.join(THIS_DIR, "patches", f"hil_gps_heading_{PX4_VERSION}.patch"),
        ],
        cwd=PX4_DIR,
    )


def build_pymavlink(message_definitions_dir: str, bell_xml_def: str) -> None:
    print2("Generating pymavlink package")

    print2("Applying Pymavlink patch")
    subprocess.check_call(["git", "reset", "--hard"], cwd=PYMAVLINK_DIR)
    subprocess.check_call(
        [
            "git",
            "apply",
            "--ignore-space-change",
            "--ignore-whitespace",
            os.path.join(THIS_DIR, "patches", f"pymavlink_{PX4_VERSION}.patch"),
        ],
        cwd=PYMAVLINK_DIR,
    )

    # copy message definitions from px4 so we're using the exact same version
    shutil.rmtree(
        os.path.join(PYMAVLINK_DIR, "message_definitions", "v1.0"),
        ignore_errors=True,
    )
    shutil.copytree(
        message_definitions_dir,
        os.path.join(PYMAVLINK_DIR, "message_definitions", "v1.0"),
    )

    pymavlink_dist_dir = os.path.join(PYMAVLINK_DIR, "dist")

    # clean the pymavlink build and target dirs
    clean_directory(pymavlink_dist_dir, [".tar.gz", ".whl"])
    clean_directory(DIST_DIR, [".tar.gz", ".whl"])

    # make a new environment with the mavlink dialect set
    new_env = os.environ.copy()
    new_env["MAVLINK_DIALECT"] = "bell"
    subprocess.check_call(
        [sys.executable, "setup.py", "sdist", "bdist_wheel"],
        cwd=PYMAVLINK_DIR,
        env=new_env,
    )

    # copy the outputs to the target directory
    for filename in os.listdir(pymavlink_dist_dir):
        shutil.copyfile(
            os.path.join(pymavlink_dist_dir, filename),
            os.path.join(DIST_DIR, filename),
        )

    # generate lua plugins for Wireshark
    # https://mavlink.io/en/guide/wireshark.html
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pymavlink.tools.mavgen",
            "--lang=WLua",
            "--wire-protocol=2.0",
            f"--output={os.path.join(DIST_DIR, 'bell-avr.lua')}",
            bell_xml_def,
        ],
        cwd=os.path.join(PYMAVLINK_DIR, ".."),
    )


def build_px4(targets: List[str], version: str) -> None:
    print2("Building PX4 firmware")

    px4_build_dir = os.path.join(PX4_DIR, "build")

    # clean the PX4 build and target dir
    clean_directory(px4_build_dir, [".px4"])
    clean_directory(DIST_DIR, [".px4"])

    for target in targets:
        subprocess.check_call(["make", target, "-j"], cwd=PX4_DIR)
        shutil.copyfile(
            os.path.join(px4_build_dir, target, f"{target}.px4"),
            os.path.join(DIST_DIR, f"{target}.{PX4_VERSION}.{version}.px4"),
        )


def container(
    should_build_pymavlink: bool,
    should_build_px4: bool,
    version: str,
    targets: List[str],
) -> None:
    # code that runs inside the container
    if PX4_VERSION < "v1.13.0":
        clone_pymavlink()

    # get px4 cloned
    clone_px4()

    # install python dependencies for pymavlink
    print2("Installing Python dependencies")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--upgrade", "pip", "wheel"]
    )
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            os.path.join(PYMAVLINK_DIR, "requirements.txt"),
        ]
    )

    # build directory paths
    if PX4_VERSION < "v1.13.0":
        message_definitions_dir = os.path.join(
            PX4_DIR,
            "mavlink",
            "include",
            "mavlink",
            "v2.0",
            "message_definitions",
        )
        generated_message_dir = os.path.join(message_definitions_dir, "..")
    else:
        message_definitions_dir = os.path.join(
            PX4_DIR,
            "src",
            "modules",
            "mavlink",
            "mavlink",
            "message_definitions",
            "v1.0",
        )
        generated_message_dir = os.path.join(message_definitions_dir, "..", "..", "..")

    bell_xml_def = os.path.join(message_definitions_dir, "bell.xml")

    print2("Injecting Bell MAVLink message")
    shutil.copyfile(os.path.join(THIS_DIR, "bell.xml"), bell_xml_def)

    # generate the mavlink C code
    if PX4_VERSION < "v1.13.0":
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pymavlink.tools.mavgen",
                "--lang=C",
                "--wire-protocol=2.0",
                f"--output={generated_message_dir}",
                bell_xml_def,
            ],
            cwd=os.path.join(PYMAVLINK_DIR, ".."),
        )

    # changes need to be committed to build
    # git config does not matter, just need *something* to commit
    subprocess.check_call(
        ["git", "config", "user.email", "github-bot@bellflight.com"], cwd=PX4_DIR
    )
    subprocess.check_call(["git", "config", "user.name", "Github Actions"], cwd=PX4_DIR)
    subprocess.check_call(["git", "add", "."], cwd=PX4_DIR)
    subprocess.check_call(
        ["git", "commit", "-m", "Local commit to facilitate build"], cwd=PX4_DIR
    )

    if should_build_pymavlink:
        build_pymavlink(message_definitions_dir, bell_xml_def)

    if should_build_px4:
        build_px4(targets, version)


def host(build_pymavlink: bool, build_px4: bool, version: str) -> None:
    # code that runs on the host operating system

    # make the target directory
    os.makedirs(DIST_DIR, exist_ok=True)

    # deal with git file permissions complaints
    subprocess.check_output(
        [
            "git",
            "config",
            "--global",
            "--add",
            "safe.directory",
            os.path.abspath(PX4_DIR),
        ]
    )

    # script to run inside the contaner
    script_cmd = (
        ["python3", "build.py"]
        + sys.argv[1:]
        + ["--container", f"--version-inner={version}"]
    )

    # select a Dcoker image
    docker_image = "docker.io/px4io/px4-dev-nuttx-focal:latest"
    if build_pymavlink and not build_px4:
        # if only building pymavlink, use a simpler ARM compatible image
        docker_image = "docker.io/library/python:3.11-buster"

    cmd = [
        "docker",
        "run",
        "--rm",
        "-w",
        "/work",
        "-v",
        f"{THIS_DIR}:/work:rw",
        docker_image,
    ] + script_cmd

    # run Docker image
    print2(f"Running: {' '.join(cmd)}")
    subprocess.check_call(cmd, stdout=sys.stdout, stderr=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a PX4/Pymavlink build")
    parser.add_argument("--container", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--inner-version", type=str, help=argparse.SUPPRESS)
    parser.add_argument("--version", type=str, default="")
    parser.add_argument(
        "--pymavlink", action="store_true", help="Build Pymavlink package"
    )
    parser.add_argument("--px4", action="store_true", help="Build PX4 firmware")
    # pixhawk v5X, v6x, v6c and NXP
    parser.add_argument(
        "--targets",
        nargs="+",
        default=[
            "px4_fmu-v5x_default",
            "px4_fmu-v6c_default",
            "px4_fmu-v6x_default",
            "nxp_fmuk66-v3_default",
        ],
    )

    args = parser.parse_args()

    if args.px4 and platform.machine() == "aarch64":
        parser.error("Sorry, cannot build PX4 on ARM")

    if args.container:
        container(args.pymavlink, args.px4, args.inner_version, args.targets)
    else:
        check_sudo()

        # if no version specified, default to git commit hash
        if not args.version:
            args.version = (
                subprocess.check_output(
                    ["git", "rev-parse", "--short", "HEAD"], cwd=THIS_DIR
                )
                .decode("utf-8")
                .strip()
            )

        host(args.pymavlink, args.px4, args.version)
