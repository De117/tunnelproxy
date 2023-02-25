import setuptools

with open("tunnelproxy/_version.py") as f:
    exec(f.read())

with open("README.md") as f:
    long_description = f.read()

setuptools.setup(
    name="tunnelproxy",
    version=__version__,
    install_requires=[
        "trio >=0.21, <0.23",
        "h11 ~=0.14.0",
    ],
    python_requires=">=3.7",
    packages=setuptools.find_packages(include=["tunnelproxy"]),
    description="A small whitelisting HTTP CONNECT proxy",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/De117/tunnelproxy",
    author="Antun Maldini",
    author_email="antun.maldini@gmail.com",
    license="MIT",
    keywords="whitelist proxy HTTP",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Framework :: Trio",
        "Intended Audience :: Developers",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: Implementation :: CPython",
        "Programming Language :: Python :: Implementation :: PyPy",
        "Topic :: Internet :: Proxy Servers",
        "Topic :: Internet :: WWW/HTTP :: HTTP Servers",
        "Topic :: Security",
        "Topic :: System :: Networking",
    ],
    project_urls={
        "Source": "https://github.com/De117/tunnelproxy",
        "Tracker": "https://github.com/De117/tunnelproxy/issues",
    },
)
