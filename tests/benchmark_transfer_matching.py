"""Synthetic #244 benchmark, run only in a throwaway fp-test-* Compose project.

Copy this file to /tmp/benchmark.py, then run it with manage.py shell:
  BENCH_MODE=seed python manage.py shell -c "exec(open('/tmp/benchmark.py').read())"
Modes: seed, refresh (full rebuild), incremental, edit, import, duplicate.
Use docker top <throwaway-app-container> -eo pid,rss,args around HTTP imports.
The seed requires an empty app database; never use production data or volumes.
"""
import os, random, time, resource, statistics, json, re, urllib.request, urllib.parse, urllib.error
from datetime import date, timedelta
from django.contrib.auth import get_user_model
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.db import connection
from finance.models import Person, Household, Membership, Account, ImportBatch, Transaction
from finance.category_services import ensure_household_categories, refresh_transfer_pairs

mode=os.environ.get('BENCH_MODE','seed')
if mode=='seed':
    random.seed(42)
    h=Household.objects.create(name='Synthetic benchmark 244')
    people=[]
    for i in range(2):
        u=get_user_model().objects.create_user(username=f'bench244_{i}',password='Synthetic-passphrase-42!')
        p=Person.objects.create(user=u,display_name=f'Synthetic {i}'); Membership.objects.create(person=p,household=h);people.append(p)
    ensure_household_categories(h)
    accounts=[]
    for i in range(10):
        shared=i<6
        accounts.append(Account.objects.create(owner=people[i%2],name=f'Synthetic {i}',account_type='checking',scope='household' if shared else 'private',household=h if shared else None,share_mode='co_owned' if shared else ''))
    batches=[ImportBatch.objects.create(account=a,imported_by=a.owner,source='huntington',source_file_sha256='a'*64,date_range_start=date(2023,10,1),date_range_end=date(2026,10,1)) for a in accounts]
    rows=[]
    # 19,370 visible to the smaller member; 68,222 to the larger.
    for i in range(72502):
        a=accounts[0] if i<15090 else accounts[6] if i<68222 else accounts[7]
        day=date(2023,10,1)+timedelta(days=random.randrange(1096))
        amount=-random.randrange(100,10000000)
        rows.append(Transaction(account=a,import_batch=batches[accounts.index(a)],transaction_date=day,amount_minor=amount,description=f'Synthetic {i}',source_row_number=i+2,fingerprint=f'{i:064x}',original_fields={}))
    Transaction.objects.bulk_create(rows,batch_size=1000)
    print('seeded',len(rows))
else:
    p=Person.objects.get(user__username='bench244_0' if mode!='edit' else 'bench244_1')
    if mode in ('refresh', 'incremental'):
        for i in range(2):
            before=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            start=time.perf_counter()
            with CaptureQueriesContext(connection) as q:
                ids = [Transaction.objects.visible_to(p).first().pk] if mode == "incremental" else None
                if ids is None:
                    refresh_transfer_pairs(p)
                else:
                    refresh_transfer_pairs(p, transaction_ids=ids)
            print(json.dumps(dict(seconds=time.perf_counter()-start,queries=len(q),rss_before_mb=before/1024,rss_peak_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024)))
    elif mode=='edit':
        c=Client(); c.force_login(p.user)
        t=Transaction.objects.visible_to(p).first()
        times=[]
        for i in range(4):
            start=time.perf_counter()
            with CaptureQueriesContext(connection) as q:
                r=c.post(f'/transactions/{t.pk}/edit/',dict(transaction_date=t.transaction_date.isoformat(),description=f'Synthetic edited {i}',amount=str(t.amount_minor/100)))
            times.append(time.perf_counter()-start);print('edit',r.status_code,times[-1],len(q))
        print('median',statistics.median(times[1:]))
    elif mode in ('import','duplicate'):
        c=Client();c.force_login(p.user)
        a=Account.objects.filter(name='Synthetic 0').first()
        content='When,Memo,Amount,Currency\n'+''.join(f'09/20/2026,Synthetic benchmark import {i},{-(i+200001)/100:.2f},USD\n' for i in range(500))
        url=f'/accounts/{a.pk}/imports/preview/'
        # Use a real session cookie and CSRF token against gunicorn, without
        # following the commit redirect (the issue times the POST itself).
        session=c.cookies['sessionid'].value
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args):return None
        opener=urllib.request.build_opener(NoRedirect)
        def send(data,ctype):
            req=urllib.request.Request('http://127.0.0.1:8000'+url,data=data,headers={'Cookie':cookie,'Content-Type':ctype,'X-CSRFToken':csrf})
            try:
                with opener.open(req) as r:return r.status,r.read().decode(),r.headers
            except urllib.error.HTTPError as e:
                if e.code!=302:raise
                return e.code,e.read().decode(),e.headers
        req=urllib.request.Request('http://127.0.0.1:8000'+url,headers={'Cookie':f'sessionid={session}'})
        with opener.open(req) as r:
            html=r.read().decode();csrf=re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"',html).group(1)
            csrfcookie=re.search(r'csrftoken=([^;]+)',str(r.headers)).group(1)
        cookie=f'sessionid={session}; csrftoken={csrfcookie}'
        boundary='SyntheticBoundary244'
        body=f'--{boundary}\r\nContent-Disposition: form-data; name="action"\r\n\r\nupload\r\n--{boundary}\r\nContent-Disposition: form-data; name="csv_file"; filename="synthetic.csv"\r\nContent-Type: text/csv\r\n\r\n{content}\r\n--{boundary}--\r\n'.encode()
        status,html,_=send(body,'multipart/form-data; boundary='+boundary)
        token=re.search(r'name="token" value="([^"]+)"',html).group(1)
        data=dict(action='commit',token=token,date_column='When',description_column='Memo',date_format='mdy_slash_4',number_format='dot_comma',amount_mode='signed',amount_column='Amount',currency_column='Currency',source='huntington',date_range_start='2026-09-01',date_range_end='2026-09-30')
        start=time.perf_counter();status,html,_=send(urllib.parse.urlencode(data).encode(),'application/x-www-form-urlencoded')
        print(mode,status,time.perf_counter()-start,'total',Transaction.objects.count())

