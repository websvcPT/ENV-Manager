# CHANGELOG


## v1.4.0 (2026-10-21)

### Documentation

- Describe the read only Compose files and the deployment names, and rework the demo scenarios for
  them
  ([`eefb1bf`](https://github.com/websvcPT/ENV-Manager/commit/eefb1bf91f58ff5eef3c53f267c5449a04b9067d))

### Features

- Let deployment names use capitals and spaces, unique in their project ignoring case
  ([`36ca664`](https://github.com/websvcPT/ENV-Manager/commit/36ca6645ad61820315fd09bfde45d05781a2b27c))

- Link Compose files to a deployment read only instead of importing them
  ([`30b22f1`](https://github.com/websvcPT/ENV-Manager/commit/30b22f17f17b9282f634e0e86996cf0ee0a9d946))

- Link every dist file the Compose import finds, the files on disk included
  ([`3b99949`](https://github.com/websvcPT/ENV-Manager/commit/3b99949463c93a27753889757d8c35e8bf6d24d3))

- Show the deployment files below the services
  ([`1219cbf`](https://github.com/websvcPT/ENV-Manager/commit/1219cbfd6fa4a90694a9573d59e71f3ccaf8df78))

### Testing

- Cover linking and reading Compose files and the import without them in the browser
  ([`815137c`](https://github.com/websvcPT/ENV-Manager/commit/815137cb3b172d1f011ff06418ef36a98f8d6d0e))


## v1.3.0 (2026-10-21)

### Features

- Give each deployment files of its own, deployed apart from the services
  ([`1991723`](https://github.com/websvcPT/ENV-Manager/commit/1991723e012378606974472aef0838fa92aed2c9))

- Keep deployment files the deployment has out of the Compose import and document them
  ([`40fac7f`](https://github.com/websvcPT/ENV-Manager/commit/40fac7f5424fcfdc95c830a894c5209605564603))

- Move API tokens from the sidebar to the Users page and the account menu
  ([`b9bf70a`](https://github.com/websvcPT/ENV-Manager/commit/b9bf70ae663efb3da659b29acc5563c08bb85594))

- Offer the deployment files and post-deploy commands in the Docker Compose import
  ([`d081143`](https://github.com/websvcPT/ENV-Manager/commit/d081143b5ba279c022845f098bf314675ccc8d6b))

- Open the new project after creating it
  ([`3912695`](https://github.com/websvcPT/ENV-Manager/commit/3912695f311c5947ff13b37f339d9ed116fd6002))

- Sort the root path picker ignoring case and filter its folders as you type
  ([`78d8efc`](https://github.com/websvcPT/ENV-Manager/commit/78d8efc6a1cd12a12add5683d26ce62bbab4f4c1))

### Testing

- Reuse the exact code that turned two-factor on when checking a reused code
  ([`6af1f2b`](https://github.com/websvcPT/ENV-Manager/commit/6af1f2b58f905304b50999363372347641e7245a))

- Wait for the count of dropped notifications instead of a fixed sleep
  ([`7052e60`](https://github.com/websvcPT/ENV-Manager/commit/7052e606d35668e228a1bfa7886cde278c24ef40))


## v1.2.0 (2026-10-20)

### Bug Fixes

- Label the deployment root as the deployment path
  ([`1b0b505`](https://github.com/websvcPT/ENV-Manager/commit/1b0b50572fd09a2162ea1c15a465474fb074e65d))

- Offer the dist file of Compose files not on disk, missing mounted files that look like files, and
  skip the deployment environment file
  ([`ff656ae`](https://github.com/websvcPT/ENV-Manager/commit/ff656ae57c377d673cb5fb24cab2d103ddaa687d))

- Remove access from any Access tab row, naming the grant that gives it, instead of an edit link
  ([`673e092`](https://github.com/websvcPT/ENV-Manager/commit/673e092ec4b4ecd8725ec7ce1fa377fd67805f5d))

### Documentation

- Describe the dist file start, missing mounted files and the deployment environment file in the
  Compose import
  ([`fdfc572`](https://github.com/websvcPT/ENV-Manager/commit/fdfc572179e1077c51dd6408be4f15a3ab095ef4))


## v1.1.0 (2026-10-20)

### Bug Fixes

- Check the name of an imported service with the service name rules
  ([`57fb61a`](https://github.com/websvcPT/ENV-Manager/commit/57fb61ab1186926fe1409438c3096aa0b27cdf0f))

- Drop the dist link of the demo certificate
  ([`8906f60`](https://github.com/websvcPT/ENV-Manager/commit/8906f602253a9bcf0b2fc473832219d340fed30e))

- Give every button and input one height per layout
  ([`aca0aa6`](https://github.com/websvcPT/ENV-Manager/commit/aca0aa64042d84c82c345d630a43c9fdcbb8e1c3))

- Give every button the same height
  ([`5cb5f5f`](https://github.com/websvcPT/ENV-Manager/commit/5cb5f5fa088f5ef78cd57f7c30171b81e95f1e20))

### Features

- Add the Docker Compose import screens to the deployment page
  ([`4794aa1`](https://github.com/websvcPT/ENV-Manager/commit/4794aa1b2284d94978f728ca36e3a08be79c6ea8))

- Import services and files from a Compose scan
  ([`4946ab1`](https://github.com/websvcPT/ENV-Manager/commit/4946ab1d141ad8588f1f7cb5514adb2cb684b225))

- Record a Compose import as the deployed baseline
  ([`c026c17`](https://github.com/websvcPT/ENV-Manager/commit/c026c179c119766055c1f8f5223ef43f2905ea7d))

- Refuse Compose includes and extends outside the deployment folder
  ([`b53f680`](https://github.com/websvcPT/ENV-Manager/commit/b53f680b59a4996c7d5114f2573827fe537e76f2))

- Scan a deployment's Compose files for services and files
  ([`f94f06a`](https://github.com/websvcPT/ENV-Manager/commit/f94f06aa123b66d34b6013278fc520710679c532))

### Refactoring

- Add a file to the session without committing it
  ([`544b146`](https://github.com/websvcPT/ENV-Manager/commit/544b146fa7cdc0921ae6da7d54ea2cd51ab2d6cf))

### Testing

- Check the import runs in the deploy history in the Compose import spec
  ([`e98023c`](https://github.com/websvcPT/ENV-Manager/commit/e98023c0d9930af85eafa66de70436d2f182b122))

- Cover the Docker Compose import flow in a browser spec
  ([`6c76488`](https://github.com/websvcPT/ENV-Manager/commit/6c764883ef6e6fb6d2d62f426a2bacf6feb43470))

- Import the demo stack into a scratch deployment and deploy it without a drift halt
  ([`25ccdaa`](https://github.com/websvcPT/ENV-Manager/commit/25ccdaae89e2df4e5ac4e5a43eda9feadb7e2451))

- Match stable parts of the Compose messages
  ([`93d2eba`](https://github.com/websvcPT/ENV-Manager/commit/93d2ebaf554cda6d7d84c4b5951277265f1f29d3))

- Pin one height for every button and input on desktop and phone
  ([`acd598b`](https://github.com/websvcPT/ENV-Manager/commit/acd598b60baa235ba4f2fd031dab7ef3005fa3e4))


## v1.0.0 (2026-10-19)

- Initial Release
