"""Real-scenario tests: the deploy flow against the demo stack on the mounted host folder.

They are not part of the default test run (``testpaths`` names ``app/tests`` only). ``make
e2e-demo`` brings the demo override up and runs them inside the app container, where the demo
folder sits at its host path.
"""
