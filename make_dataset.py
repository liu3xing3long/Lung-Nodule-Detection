import json
import glob

src = './json/1/'
tar = './json/1/'

src_ext = 'txt'
tar_ext = 'json'


files = glob.glob(fr'{src}/*.{src_ext}')
for fn in files:
    print(fn)

    with open(fn, 'r') as fp:
        suids = fp.readlines()

        outputsuids = []
        for suid in suids:
            outputsuids.append(suid.replace("\n", ""))
        with open(fn.replace(src_ext, tar_ext), 'w+') as fp2:
            json.dump(outputsuids, fp2)