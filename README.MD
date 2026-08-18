# Stable release: 1.6.1

#### Docker Image Build Status
[![Docker Image Build For SiteRM FE](https://github.com/sdn-sense/siterm/actions/workflows/docker-image-fe.yml/badge.svg)](https://github.com/sdn-sense/siterm/actions/workflows/docker-image-fe.yml)
[![Docker Image Build For SiteRM Agent](https://github.com/sdn-sense/siterm/actions/workflows/docker-image-agent.yml/badge.svg)](https://github.com/sdn-sense/siterm/actions/workflows/docker-image-agent.yml)
#### DeepSource Code Analyze Statistics
[![DeepSource](https://deepsource.io/gh/sdn-sense/siterm.svg/?label=active+issues&show_trend=true&token=33jNqEPsCU2obfP3cXwIUj7m)](https://deepsource.io/gh/sdn-sense/siterm/?ref=repository-badge)
[![DeepSource](https://deepsource.io/gh/sdn-sense/siterm.svg/?label=resolved+issues&show_trend=true&token=33jNqEPsCU2obfP3cXwIUj7m)](https://deepsource.io/gh/sdn-sense/siterm/?ref=repository-badge)
#### CodeFactor
[![CodeFactor](https://www.codefactor.io/repository/github/sdn-sense/siterm/badge)](https://www.codefactor.io/repository/github/sdn-sense/siterm)



## Endpoint pool exhaustion (draft note)

The allocator takes an IPv6 /64 from the SENSE-O pool and marks the row
allocated. On exhaustion it raises and the request goes to FAILED:

```python
def allocate(self, site):
    ep = self.db.query(Endpoint).filter_by(site=site).first()
    if ep.is_allocated:
        raise ValueError("already in use")
    ep.is_allocated = True
    return ep
```

Cleanup swallows its own failures at DEBUG, so leaks accumulate silently.
