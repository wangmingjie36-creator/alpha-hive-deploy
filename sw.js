// Alpha Hive Service Worker - 2026-03-03
var CACHE_NAME='alpha-hive-2026-03-03';
var PRECACHE_URLS=['./', 'index.html', 'manifest.json',
  'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js'];

self.addEventListener('install', function(e){
  e.waitUntil(
    caches.open(CACHE_NAME).then(function(cache){
      return cache.addAll(PRECACHE_URLS);
    })
  );
});

self.addEventListener('activate', function(e){
  e.waitUntil(
    caches.keys().then(function(names){
      return Promise.all(
        names.filter(function(n){ return n!==CACHE_NAME; })
             .map(function(n){ return caches.delete(n); })
      );
    })
  );
});

self.addEventListener('fetch', function(e){
  var url=new URL(e.request.url);
  // JSON 数据用 network-first
  if(url.pathname.endsWith('.json')){
    e.respondWith(
      fetch(e.request).then(function(r){
        var rc=r.clone();
        caches.open(CACHE_NAME).then(function(c){ c.put(e.request, rc); });
        return r;
      }).catch(function(){ return caches.match(e.request); })
    );
    return;
  }
  // HTML/CDN 用 cache-first
  e.respondWith(
    caches.match(e.request).then(function(r){
      return r || fetch(e.request).then(function(resp){
        var rc=resp.clone();
        caches.open(CACHE_NAME).then(function(c){ c.put(e.request, rc); });
        return resp;
      });
    })
  );
});
