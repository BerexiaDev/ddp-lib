from setuptools import setup, find_packages

setup(
    name='cmr_etl_lib',
    version='0.1',
    packages=find_packages(),
    install_requires=[
        "cryptography==44.0.2",
        "flask-restx==1.2.0",
        "inject==5.0.0",
        "python-dotenv==0.21.1",
        "PyJWT==1.5.3",
        "loguru==0.6.0",
        "Flask-Bcrypt==1.0.1",
    ],
    author='Berexia dev',
    author_email='berexiadev@berexia.com',
    description='CMR ETL Global Library',
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
    ],
    python_requires='>=3.6',
)
