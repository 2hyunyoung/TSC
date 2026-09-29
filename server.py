from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import csv, io, json, os, sqlite3, secrets, urllib.request, urllib.parse
import openpyxl

ROOT=Path(__file__).parent
DB=ROOT/'tsc_local.db'

def load_dotenv():
    env_file=ROOT/'.env'
    if not env_file.exists(): return
    for line in env_file.read_text(encoding='utf-8-sig').splitlines():
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        key,value=line.split('=',1)
        key=key.strip();value=value.strip().strip('"').strip("'")
        os.environ[key]=value

load_dotenv()
SUPABASE_URL=os.getenv('SUPABASE_URL','').rstrip('/')
SUPABASE_KEY=os.getenv('SUPABASE_SERVICE_ROLE_KEY','')
SUPABASE_ANON_KEY=os.getenv('SUPABASE_ANON_KEY',SUPABASE_KEY)
DEMO_PASSWORD=os.getenv('TSC_DEMO_PASSWORD','tsc1234')

def supabase_request(method,path,payload=None,token=None):
    if not SUPABASE_URL or not SUPABASE_KEY: return None
    body=None if payload is None else json.dumps(payload,ensure_ascii=False).encode('utf-8')
    req=urllib.request.Request(SUPABASE_URL+path,data=body,method=method,headers={'apikey':SUPABASE_KEY,'Authorization':'Bearer '+(token or SUPABASE_KEY),'Content-Type':'application/json','Prefer':'resolution=ignore-duplicates,return=minimal'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req,timeout=15) as res:
        raw=res.read();return json.loads(raw.decode('utf-8')) if raw else None

def db():
    con=sqlite3.connect(DB);con.row_factory=sqlite3.Row
    con.execute('CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY AUTOINCREMENT, sr_number TEXT UNIQUE, payload TEXT NOT NULL, source TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    con.execute('CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)');con.commit();return con

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(ROOT),**kwargs)
    def headers_json(self): return {'Content-Type':'application/json; charset=utf-8','Access-Control-Allow-Origin':'*'}
    def send_json(self,value,status=200):
        body=json.dumps(value,ensure_ascii=False).encode('utf-8');self.send_response(status)
        for k,v in self.headers_json().items():self.send_header(k,v)
        self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def read_json(self): return json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))).decode('utf-8'))
    def do_OPTIONS(self): self.send_response(204);self.send_header('Access-Control-Allow-Origin','*');self.send_header('Access-Control-Allow-Headers','Content-Type, Authorization, X-File-Name');self.end_headers()
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/api/calls':
            if SUPABASE_URL and SUPABASE_KEY:
                try:
                    result=supabase_request('GET','/rest/v1/calls?select=payload&order=created_at.desc') or []
                    return self.send_json([x['payload'] for x in result])
                except Exception as exc: return self.send_json({'error':f'Supabase 콜 데이터 조회 실패: {exc}'},502)
            con=db();rows=[json.loads(x['payload']) for x in con.execute('SELECT payload FROM calls ORDER BY json_extract(payload,\'$.SR Date\') DESC')];con.close();return self.send_json(rows)
        if path=='/api/audit':
            con=db();rows=[json.loads(x['payload']) for x in con.execute('SELECT payload FROM audit_logs ORDER BY id DESC')];con.close();return self.send_json(rows)
        return super().do_GET()
    def do_POST(self):
        path=urlparse(self.path).path
        try:
            if path=='/api/login':
                body=self.read_json();email=body.get('email','').strip();password=body.get('password','')
                if SUPABASE_URL and SUPABASE_ANON_KEY:
                    req=urllib.request.Request(SUPABASE_URL+'/auth/v1/token?grant_type=password',data=json.dumps({'email':email,'password':password}).encode('utf-8'),method='POST',headers={'apikey':SUPABASE_ANON_KEY,'Content-Type':'application/json'})
                    try:
                        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                        with opener.open(req,timeout=15) as res:
                            auth=json.loads(res.read().decode('utf-8'))
                        return self.send_json({'token':auth['access_token'],'user':{'email':auth.get('user',{}).get('email',email),'id':auth.get('user',{}).get('id'),'role':'operator'}})
                    except Exception: return self.send_json({'error':'Supabase 로그인에 실패했습니다.'},401)
                if not email or password!=DEMO_PASSWORD:return self.send_json({'error':'이메일 또는 비밀번호가 올바르지 않습니다.'},401)
                return self.send_json({'token':secrets.token_urlsafe(24),'user':{'email':email,'role':'operator'}})
            if path=='/api/calls':
                body=self.read_json();rows=body.get('rows',[]);accepted=0
                if SUPABASE_URL and SUPABASE_KEY:
                    records=[{'sr_number':row.get('SR Number') or f"LOCAL-{secrets.token_hex(6)}",'payload':row,'source':body.get('source','upload'),'created_by':(body.get('user') or {}).get('email')} for row in rows]
                    try:
                        supabase_request('POST','/rest/v1/calls',records,token=self.headers.get('Authorization','').replace('Bearer ',''));return self.send_json({'accepted':len(records)})
                    except Exception as exc: return self.send_json({'error':f'Supabase 콜 데이터 저장 실패: {exc}'},502)
                con=db()
                for row in rows:
                    sr=row.get('SR Number') or f"LOCAL-{secrets.token_hex(6)}"
                    row['SR Number']=sr
                    try: con.execute('INSERT INTO calls(sr_number,payload,source) VALUES(?,?,?)',(sr,json.dumps(row,ensure_ascii=False),body.get('source','upload')));accepted+=1
                    except sqlite3.IntegrityError: pass
                con.commit();con.close();return self.send_json({'accepted':accepted})
            if path=='/api/audit':
                payload=self.read_json()
                if SUPABASE_URL and SUPABASE_KEY:
                    try: supabase_request('POST','/rest/v1/audit_logs',[{'payload':payload}],token=self.headers.get('Authorization','').replace('Bearer ',''));return self.send_json({'ok':True})
                    except Exception as exc: return self.send_json({'error':f'Supabase 감사 로그 저장 실패: {exc}'},502)
                con=db();con.execute('INSERT INTO audit_logs(payload) VALUES(?)',(json.dumps(payload,ensure_ascii=False),));con.commit();con.close();return self.send_json({'ok':True})
            if path=='/api/upload':
                payload=self.rfile.read(int(self.headers.get('Content-Length','0')));name=self.headers.get('X-File-Name','upload.xlsx').lower()
                if name.endswith(('.xlsx','.xlsm','.xltx','.xltm')):
                    wb=openpyxl.load_workbook(io.BytesIO(payload),read_only=True,data_only=True);sheet=wb[wb.sheetnames[0]];rows=list(sheet.iter_rows(values_only=True));wb.close();out=io.StringIO();csv.writer(out,lineterminator='\n').writerows(rows);payload=out.getvalue().encode('utf-8-sig')
                self.send_response(200);self.send_header('Content-Type','text/csv; charset=utf-8');self.send_header('Access-Control-Allow-Origin','*');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload);return
            self.send_error(404)
        except Exception as exc: self.send_json({'error':str(exc)},400)

if __name__=='__main__':
    print(f'Supabase configured: {bool(SUPABASE_URL and SUPABASE_KEY)}',flush=True)
    print('TSC server running at http://localhost:8765',flush=True);ThreadingHTTPServer(('localhost',8765),Handler).serve_forever()
