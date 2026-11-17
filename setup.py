from setuptools import setup, find_packages

setup(
    name='ddp_lib',
    version='0.1',
    packages=find_packages(),
    install_requires=[
        # Security floors (CVE fixes); exact versions are locked by each
        # service's constraints.txt, not here.
        "cryptography>=50.0.0",
        "flask-restx>=1.3.2,<2",  # first release supporting Flask 3 / Werkzeug 3
        "inject==5.0.0",
        "python-dotenv>=1.2.2,<2",
        "PyJWT>=2.15.0,<3",
        "loguru==0.6.0",
        "Flask-Bcrypt==1.0.1",
    ],
    author='Berexia dev',
    author_email='berexiadev@berexia.com',
    description='Deepkube Data Platform Global Library',
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
    ],
    python_requires='>=3.6',
)
