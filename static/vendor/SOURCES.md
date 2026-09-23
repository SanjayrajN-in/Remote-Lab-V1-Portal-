# Vendored third-party assets

These were previously loaded from public CDNs at page load. That meant the
portal executed whatever those CDNs returned, with no integrity check, and
it meant the lab pages could not work at all on a closed network - which is
where this portal is meant to run.

They are now served from this directory. To update one, re-download it from
the URL below, replace the file, and update the checksum here.

| File | Upstream |
|---|---|
| `socket.io-4.7.5.min.js` | https://cdn.socket.io/4.7.5/socket.io.min.js |
| `chart-4.5.1.umd.min.js` | https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js |
| `chartjs-plugin-zoom-2.0.1.min.js` | https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js |
| `katex/` | KaTeX 0.18.1, already vendored here before this change |
| `fontawesome-6.0.0.min.css` | https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css |
| `fa-webfonts/` | https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/ (woff2 only) |
| `google-fonts.css` | https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=Roboto+Mono:wght@400;700&display=swap |
| `gfonts/` | the fonts.gstatic.com files that stylesheet referenced |

KaTeX was already served from `katex/` on the lab page, but the experiment
page was still fetching 0.16.9 from a CDN - so the two pages ran different
versions of it. Both now use the local 0.18.1.

Chart.js was previously requested with **no version at all**
(`/npm/chart.js`), so the portal silently took whatever major version the
CDN served that day. It is pinned at 4.5.1, which is what that URL resolved
to when this was vendored.

The three CSS files have been edited in one way only: font URLs repointed at
the local copies, and the `woff`/`ttf` fallbacks dropped in favour of
`woff2`, which every browser since 2016 supports. Nothing else was changed.

## Checksums

Verify with: `sha256sum -c CHECKSUMS.txt`
