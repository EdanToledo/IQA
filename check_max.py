import jsonlines
import os

def check_max(FILENAME,qa_weight=0.5):
    max = 0
    max_obj = None
    suff_weight = 1-qa_weight
    with jsonlines.open(FILENAME) as reader:
        for obj in reader:
            val = (qa_weight*obj["qa"]+ suff_weight*obj["sufficient info"])
            if val > max:
                max = val
                max_obj = obj
        
    print("Acc:",max_obj["qa"],"Sufficient Info:",max_obj["sufficient info"])

def extract_details(filename):
    model = filename[:filename.index("_")]
   
    filename= filename[filename.index("_")+1:]
    question = filename[:filename.index("_")]
    
    filename= filename[filename.index("_")+1:]
    rand = bool(filename[filename.index("_")-1])
    
    try:
        filename.index("size")
        num_games = filename[filename.index("size")+4:-5]
    except:
        num_games = "unlimited"
    

    print(model,question,"Random" if rand else "Fixed",num_games)
    
def extract_dir_max(a_directory,qa_weight=0.5):

    for filename in os.listdir(a_directory):
        filepath = os.path.join(a_directory, filename)
        print("===========================================================================")
        extract_details(filename)
        check_max(filepath,qa_weight)
        print("===========================================================================")

if __name__=="__main__":
    extract_dir_max("../qaitlogs")