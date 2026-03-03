from setuptools import setup, find_packages

setup(
    name="video-agent-hero",
    version="0.1.0",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "vah=cli.main:main",
        ],
    },
    python_requires=">=3.11",
)
