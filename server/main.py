"""Standalone stateless read-only API. python3 -m server.main --port 8775 --data-dir ..."""
from __future__ import annotations
import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from . import __version__
from . import chain
from .catalog import catalog
from .errors import APIError

MAX_BODY=600*1024

class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,port=8775,data_dir=None,public_origin=None):
        origin=public_origin if public_origin is not None else os.environ.get('PUBLIC_ORIGIN')
        if origin:
            parsed=urlsplit(origin)
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or origin.endswith('/'):
                raise ValueError('PUBLIC_ORIGIN must be an exact HTTPS origin without path or credentials')
        self.public_origin=origin
        if data_dir:
            # Independent reserved runtime directory; no catalog/user/chain history is persisted.
            path=Path(data_dir).resolve();path.mkdir(parents=True,exist_ok=True)
            self.data_dir=path
        else:self.data_dir=None
        super().__init__(('127.0.0.1',port),Handler)
        port=self.server_address[1]
        self.allowed_hosts={f'127.0.0.1:{port}',f'localhost:{port}'}
        self.allowed_origins={origin} if origin else {f'http://127.0.0.1:{p}' for p in [port,5202,5212]} | {f'http://localhost:{p}' for p in [port,5202,5212]}
        demo_origin=os.environ.get('DEMO_ORIGIN','').strip()
        if demo_origin:
            self.allowed_origins=set(self.allowed_origins); self.allowed_origins.add(demo_origin)

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    server_version='JiangchengChain/1.0'
    def setup(self):
        super().setup();self.connection.settimeout(15)
    def log_message(self,fmt,*args):
        # Neither transaction query strings, request bodies nor snapshot fields are logged.
        pass
    def guard(self):
        hosts=self.headers.get_all('Host',[])
        if len(hosts)!=1 or hosts[0].lower() not in self.server.allowed_hosts:raise APIError('拒绝未配置的 Host',403)
        origins=self.headers.get_all('Origin',[])
        if len(origins)>1 or (origins and origins[0] not in self.server.allowed_origins):raise APIError('拒绝非同源请求',403)
        if self.command=='POST' and self.server.public_origin and origins!=[self.server.public_origin]:raise APIError('POST 请求必须来自已配置同源页面',403)
        if self.headers.get('Sec-Fetch-Site')=='cross-site':raise APIError('拒绝跨站 API 请求',403)
    def response(self,body,status=200):
        raw=json.dumps(body,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
        self.send_response(status)
        for key,value in {'Content-Type':'application/json; charset=utf-8','Content-Length':str(len(raw)),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'no-referrer'}.items():self.send_header(key,value)
        origin=self.headers.get('Origin')
        if origin in self.server.allowed_origins:self.send_header('Access-Control-Allow-Origin',origin);self.send_header('Vary','Origin')
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(raw)
    def body(self):
        if self.headers.get('Transfer-Encoding'):raise APIError('不支持分块请求体')
        sizes=self.headers.get_all('Content-Length',[])
        if len(sizes)!=1 or not re.fullmatch(r'[0-9]{1,9}',sizes[0]):raise APIError('需要有效的单个 Content-Length',411)
        length=int(sizes[0])
        if length>MAX_BODY:
            # Drain (bounded) the over-limit body on the existing connection so we can
            # write a clean 413 response instead of resetting mid-write. The hard 600 KiB
            # cap still applies: we never accept or parse the oversized payload.
            to_drain=min(length,MAX_BODY+4096)
            try:self.rfile.read(to_drain)
            except OSError:pass
            raise APIError('请求超过 600 KiB 上限',413)
        if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/json':raise APIError('只接受 application/json',415)
        raw=self.rfile.read(length)
        if len(raw)!=length:raise APIError('请求体不完整')
        try:data=json.loads(raw.decode(),parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite')))
        except (UnicodeDecodeError,ValueError,RecursionError) as exc:raise APIError('请求不是有效的 UTF-8 JSON') from exc
        if not isinstance(data,dict):raise APIError('请求必须为 JSON 对象')
        return data
    def dispatch(self):
        try:
            self.guard();parts=urlsplit(self.path)
            if parts.query or parts.fragment:raise APIError('API 不接受查询字符串')
            path=parts.path
            if self.command in {'GET','HEAD'}:
                if path=='/api/health':return self.response({'ok':True,'project':'江城链察','version':__version__})
                if path=='/api/catalog':return self.response(catalog())
                raise APIError('接口不存在',404)
            if self.command=='POST':
                if path not in {'/api/investigate','/api/verify'}:raise APIError('接口不存在',404)
                data=self.body()
                return self.response(chain.investigate(data) if path=='/api/investigate' else chain.verify(data))
            raise APIError('方法不支持',405)
        except APIError as exc:
            # Close on rejected bodies so unread bytes cannot become another request.
            self.close_connection=True
            self.response({'error':str(exc)},exc.status)
        except (BrokenPipeError,ConnectionResetError,TimeoutError):self.close_connection=True
        except Exception:
            self.close_connection=True;self.response({'error':'服务处理失败；未生成或保存调查结论。'},500)
    do_GET=do_HEAD=do_POST=do_PUT=do_DELETE=do_OPTIONS=dispatch

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8775)
    parser.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parent/'data')
    args=parser.parse_args()
    server=Server(args.port,args.data_dir)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()

if __name__=='__main__':main()
