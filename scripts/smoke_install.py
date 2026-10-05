"""Build and test wheel and sdist in fresh environments (requires build, twine)."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def run(*args, cwd):
    # Run a command. Stop the script if it fails.
    subprocess.run([str(arg) for arg in args], cwd=cwd, check=True)


def main():
    # Option: which Python to test with (default: the current one)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Runtime to test")
    args = parser.parse_args()

    # Project root folder (ddp-connectors/)
    project = Path(__file__).resolve().parents[1]

    # Temporary folder, deleted at the end
    with tempfile.TemporaryDirectory(prefix="ddp-smoke-") as directory:
        work = Path(directory)
        dist = work / "dist"

        # Build the sdist, then the wheel from that sdist
        run(sys.executable, "-m", "build", "--outdir", dist, project, cwd=work)

        # Expect exactly 2 files: one wheel and one sdist
        artifacts = sorted(dist.iterdir())
        if len(artifacts) != 2:
            raise RuntimeError("Expected exactly one wheel and one sdist")

        # Check the package information
        run(sys.executable, "-m", "twine", "check", "--strict", *artifacts, cwd=work)

        # Test each file in its own clean environment
        for index, artifact in enumerate(artifacts):
            # Create a new empty virtual environment
            env = work / f"runtime-{index}"
            run(args.python, "-m", "venv", env, cwd=work)

            # Python inside that environment (Windows or Linux/macOS)
            python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

            # Install the package and its dependencies
            run(python, "-m", "pip", "install", artifact, cwd=work)

            # Check that the installed libraries fit together
            run(python, "-m", "pip", "check", cwd=work)

            # Run the tests (-I = use only the installed package)
            run(python, "-I", "-m", "unittest", "discover", "-v", "-s",
                project / "tests", cwd=work)

            # List installed libraries (useful for debugging)
            run(python, "-m", "pip", "freeze", cwd=work)

        print("Wheel and sdist clean-install checks passed", flush=True)


if __name__ == "__main__":
    main()