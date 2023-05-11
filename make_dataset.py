import json
import glob
import os


input_file = './json/all/all.txt'
outputpath = './json/'
input_all_suids = []
with open(input_file, 'rt') as fp:
    suids = fp.readlines()
    for suid in suids:
        input_all_suids.append(suid.replace("\n", "").replace('.mhd', ''))

for ii in range(5):
    thisoutputpath = fr'{outputpath}/{ii + 1}'
    
    trainset = []
    valset = []
    for isuid in input_all_suids:
        keyword = fr'subset{ii * 2}'
        keyword2 = fr'subset{ii * 2 + 1}'

        thissuid = isuid[isuid.index('/') + 1: ]
        if keyword in isuid or keyword2 in isuid:
            valset.append(thissuid)
        else:
            trainset.append(thissuid)

    if not os.path.exists(thisoutputpath):
        os.makedirs(thisoutputpath)
    
    print(len(trainset), len(valset))
    with open(fr"{thisoutputpath}/LUNA_train.json", 'wt') as fp:
        json.dump(trainset, fp)
    with open(fr"{thisoutputpath}/LUNA_test.json", 'wt') as fp:
        json.dump(valset, fp)
    with open(fr"{thisoutputpath}/LUNA_val.json", 'wt') as fp:
        json.dump(valset, fp)
