import http.client
import json
import tempfile
import threading
import unittest
from server.main import Server,MAX_BODY

class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.server=Server(0,cls.temp.name,public_origin='https://wutiantian.cn')
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start();cls.port=cls.server.server_address[1]
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()
    def request(self,path,method='GET',body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
        if isinstance(body,dict):body=json.dumps(body).encode()
        c.request(method,path,body=body,headers={'Content-Type':'application/json',**(headers or {})})
        r=c.getresponse();status=r.status;data=r.read();c.close();return status,json.loads(data) if data else None
    def test_health_and_catalog(self):
        self.assertEqual(self.request('/api/health')[0],200)
        status,catalog=self.request('/api/catalog');self.assertEqual(status,200);self.assertEqual(len(catalog['agents']),5);self.assertEqual(len(catalog['skills']),15)
    def test_post_requires_origin(self):
        self.assertEqual(self.request('/api/investigate','POST',{'mode':'demo'})[0],403)
    def test_bad_origin(self):
        self.assertEqual(self.request('/api/catalog',headers={'Origin':'https://evil.invalid'})[0],403)
    def test_bad_host(self):
        self.assertEqual(self.request('/api/catalog',headers={'Host':'evil.invalid'})[0],403)
    def test_cross_site(self):
        self.assertEqual(self.request('/api/catalog',headers={'Sec-Fetch-Site':'cross-site'})[0],403)
    def test_demo_then_verify_http(self):
        headers={'Origin':'https://wutiantian.cn'}
        status,r=self.request('/api/investigate','POST',{'mode':'demo'},headers);self.assertEqual(status,200)
        status,v=self.request('/api/verify','POST',{'receipt':r},headers);self.assertEqual(status,200);self.assertTrue(v['valid'])
    def test_malformed_and_invalid_hash(self):
        h={'Origin':'https://wutiantian.cn'}
        self.assertEqual(self.request('/api/investigate','POST',b'{',h)[0],400)
        self.assertEqual(self.request('/api/investigate','POST',{'mode':'live','transaction_hash':'xyz'},h)[0],400)
        self.assertEqual(self.request('/api/investigate','POST',{'mode':[]},h)[0],400)
    def test_body_limit(self):
        self.assertEqual(self.request('/api/verify','POST',b'x'*(MAX_BODY+1),{'Origin':'https://wutiantian.cn'})[0],413)
    def test_query_and_method_rejected(self):
        self.assertEqual(self.request('/api/catalog?url=x')[0],400)
        self.assertEqual(self.request('/api/catalog','DELETE')[0],405)
    def test_invalid_public_origin(self):
        for value in ['https://wutiantian.cn/path','http://wutiantian.cn','https://u:p@wutiantian.cn','https://wutiantian.cn/']:
            with self.assertRaises(ValueError):Server(0,public_origin=value)

if __name__=='__main__':unittest.main()
