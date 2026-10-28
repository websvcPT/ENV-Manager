# CHANGELOG


## v1.5.0 (2026-10-28)

### Bug Fixes

- Audit opening logs once per filter in ten minutes and mask the previous value too
  ([`7f114d0`](https://github.com/websvcPT/ENV-Manager/commit/7f114d0ca1e6bfa0c63888862b8347be3e2ea92b))

- Create an account at the first SSO sign in only for a verified email
  ([`34c965c`](https://github.com/websvcPT/ENV-Manager/commit/34c965c84211798adf333b3d16447e0851897377))

- Drop the Superadmin tags from tabs and page titles
  ([`c89f98f`](https://github.com/websvcPT/ENV-Manager/commit/c89f98fa8f862b0b70d4a4d2b893ee4251844673))

- Give Test login a lock icon and name what a pull reports once a login is removed
  ([`f27d370`](https://github.com/websvcPT/ENV-Manager/commit/f27d3700a4fb61c3f2721a118583fba3cb9e7b5f))

- Keep the last row of the status strip at the width of the other cells
  ([`7b22213`](https://github.com/websvcPT/ENV-Manager/commit/7b2221309f134d212980064b774ff5fbfa1481c1))

- Keep the line count, Following and download of the Logs toolbar on one row on a phone
  ([`d6d91d8`](https://github.com/websvcPT/ENV-Manager/commit/d6d91d81717e30d7f9dad13cb1f0606aec0378a3))

- Keep the login card from saying who may sign in with a password
  ([`6d57241`](https://github.com/websvcPT/ENV-Manager/commit/6d572410b4255e3cd5412bede78a9ea66b2fca0e))

- Land on Settings after creating a deployment and widen the phone tab targets
  ([`42493af`](https://github.com/websvcPT/ENV-Manager/commit/42493af44bd8b0104765575efb9b533713f698f9))

- Mark a container to recreate after any run that wrote a file
  ([`1d10a01`](https://github.com/websvcPT/ENV-Manager/commit/1d10a01cccc24150343b3c04c7f4f1014afb1528))

- Show the registry's own reason when Test login is refused
  ([`844ecb6`](https://github.com/websvcPT/ENV-Manager/commit/844ecb62e1a471b749ba426aaf9279e1abcb07d7))

- Stop the detected stacks table from squeezing the stack and Compose file names
  ([`574e1ad`](https://github.com/websvcPT/ENV-Manager/commit/574e1add1222d4d78355fba573505d5983e6d362))

- Stop the section tabs from overlapping when they scroll on a phone
  ([`33462a1`](https://github.com/websvcPT/ENV-Manager/commit/33462a1df6c5cb1409a3664ecbeef84353ca3aaa))

### Documentation

- Capture the README screenshots from a neutral deployment path
  ([`8365d7d`](https://github.com/websvcPT/ENV-Manager/commit/8365d7d687b8dffe1332a89a0a313b5fd6b2d649))

- Describe Docker registry logins
  ([`77fc1b9`](https://github.com/websvcPT/ENV-Manager/commit/77fc1b989a6f9129f066abbc553b68ee1b47dd02))

- Describe the deployment tabs, stack runtime, logs and single sign-on
  ([`fe8bbce`](https://github.com/websvcPT/ENV-Manager/commit/fe8bbcec97d96538d4d01302f46abb25423c2186))

- List the features in groups in the README
  ([`ea5e06c`](https://github.com/websvcPT/ENV-Manager/commit/ea5e06cc51906dba6adec470e34b312c5216c0f7))

- Show the deployment overview and logs in the README screenshots
  ([`483d6ab`](https://github.com/websvcPT/ENV-Manager/commit/483d6ab4b055b377ceb73a4e06fc38beeb6a9c60))

- Update readme
  ([`a186dc8`](https://github.com/websvcPT/ENV-Manager/commit/a186dc8456e4506d733ff0d23bd59d4eae6a29f2))

### Features

- Configure single sign-on in a Settings tab
  ([`f601c22`](https://github.com/websvcPT/ENV-Manager/commit/f601c223bb2569c3c2200e6c933de2463a5f24d9))

- Create a deployment from a stack found on the host
  ([`d0ff8d2`](https://github.com/websvcPT/ENV-Manager/commit/d0ff8d2004c2349265fa27a5bf88ba4e605fd5bc))

- Follow a deployment's container logs with its values masked
  ([`4d7d8ff`](https://github.com/websvcPT/ENV-Manager/commit/4d7d8ffe12aaad8b7f319054303e30e22cceeb8b))

- Manage Docker registry logins in Settings and test them
  ([`0fb5b27`](https://github.com/websvcPT/ENV-Manager/commit/0fb5b27faa6ef2d8218d9d60fd1a9ad8011fdebb))

- Name item types in the breadcrumb and drop the parent line from item headers
  ([`dcf74a2`](https://github.com/websvcPT/ENV-Manager/commit/dcf74a2c70417e49bd19b87321deb5bfa40028d4))

- Read the state, uptime and network of a deployment's containers
  ([`6db59e7`](https://github.com/websvcPT/ENV-Manager/commit/6db59e7f6ccf2834c5d406bd17d5311827ca5738))

- Run Keycloak in development and prove single sign-on in the browser
  ([`e199da8`](https://github.com/websvcPT/ENV-Manager/commit/e199da84b25bc40f34bc451ae3b7a5fa467ce168))

- Run post-deploy commands with the stored registry logins
  ([`0af1e70`](https://github.com/websvcPT/ENV-Manager/commit/0af1e7098576b6800cf4e7b415414eee982b549a))

- Show the containers, uptime and network of a deployment on its Overview
  ([`44b542f`](https://github.com/websvcPT/ENV-Manager/commit/44b542f43303d49cd74e1b4913f56e8adfd3ad23))

- Sign in through an OpenID Connect provider (service and storage)
  ([`7c00dd4`](https://github.com/websvcPT/ENV-Manager/commit/7c00dd45fbe815b107af8b70ad5e2de971eea669))

- Sign in with SSO from the login page and keep password sign in for superadmins
  ([`1f3c162`](https://github.com/websvcPT/ENV-Manager/commit/1f3c1621ec72f744d3bc71197ab00de6a0ec954b))

- Split the deployment page into Overview, Files, History and Settings tabs
  ([`f251a4d`](https://github.com/websvcPT/ENV-Manager/commit/f251a4dd064d2a6a475417e8f78a27653efeee4c))

- Start, stop, restart and recreate a service's container from the Overview
  ([`af3fe4c`](https://github.com/websvcPT/ENV-Manager/commit/af3fe4c55654f33b939abb40460f7d739e4a7510))

- Store Docker registry logins with sealed tokens
  ([`126c25e`](https://github.com/websvcPT/ENV-Manager/commit/126c25e0c2e6b7f9b1c9589f1c274b705aa7894b))

- Store the Compose project name and find stacks on the host
  ([`db6b522`](https://github.com/websvcPT/ENV-Manager/commit/db6b522ea3c208a3c6e0706b6c85a19b8f9e3b0b))

- Turn masking of the container logs on or off in Settings
  ([`1baafb6`](https://github.com/websvcPT/ENV-Manager/commit/1baafb65f349b74b54eab59be7d933e94bad26a6))

### Testing

- Leave content digests and timestamps out of the text searched for leaked values
  ([`1d31131`](https://github.com/websvcPT/ENV-Manager/commit/1d31131b499ab505607e66a272501413ad11a32f))

- Match a leaked value as its own token so a short port is not found inside a timestamp
  ([`4850141`](https://github.com/websvcPT/ENV-Manager/commit/48501419c1e59413a3a45014a75f0fb540dc12dc))

- Pin the answers of the container action routes and cover a refused action in the browser
  ([`84e2fcb`](https://github.com/websvcPT/ENV-Manager/commit/84e2fcbeef071dfa00f782db215ea08624eef928))

- Pin the reason a malformed set of signing keys is refused with
  ([`ee7698e`](https://github.com/websvcPT/ENV-Manager/commit/ee7698e497faab3c602cd4f466586eda98ed7155))

- Prove registry logins against a local registry
  ([`a6f99c1`](https://github.com/websvcPT/ENV-Manager/commit/a6f99c1e7166cfdc8af1db7e88e5cee3e5e05677))

- Tolerate a process that exits while its status file is read
  ([`e4b79dc`](https://github.com/websvcPT/ENV-Manager/commit/e4b79dc7bf72bea72649add2666bc91f51a329fe))

- Wait for queued notifications before counting the statements of a request
  ([`8d55118`](https://github.com/websvcPT/ENV-Manager/commit/8d55118a3034693bf75d68b8e26db358320072db))


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
