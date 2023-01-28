from setuptools import setup

APP = ["testnet.py"]
DATA_FILES = ['jkswallet.png']
OPTIONS = {
    'iconfile': 'jkswalletICON.icns',
    'argv_emulation': True,
    'plist': {
        'CFBundleName': 'JKSwallet',
        'CFBundleDisplayName': 'JKSwallet',
        'CFBundleGetInfoString': 'JKSwallet',


    }
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app']
)

