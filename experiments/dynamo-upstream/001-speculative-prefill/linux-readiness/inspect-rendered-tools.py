import json
from pathlib import Path
from tokenizers import Tokenizer
root=Path('/work')
tokenizer=Tokenizer.from_file(str(root/'model/tokenizer.json'))
rows=[]
for client,folder in [('claude','protocol-attempt2'),('codex','protocol-codex')]:
 for i in (1,2):
  raw=json.loads((root/'requests'/f'{client}-{i}.json').read_text())
  backend=json.loads((root/folder/f'backend-{i}.json').read_text())
  text=tokenizer.decode(backend['token_ids'],skip_special_tokens=False)
  (root/folder/f'rendered-{i}.txt').write_text(text)
  names=[]
  for t in raw.get('tools',[]):
   if t.get('type')=='namespace':
    names.extend((t['name']+'.'+f['name'],f['name']) for f in t.get('tools',[]))
   elif 'name' in t:names.append((t['name'],t['name']))
  rows.append({'client':client,'request':i,'name_text_presence':{n:s in text for n,s in names}})
print(json.dumps(rows,indent=2))
