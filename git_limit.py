"""Linux-only child wrapper. Limits apply before Git reads untrusted pack data."""
import os
import resource
import sys

resource.setrlimit(resource.RLIMIT_AS, (512 << 20, 512 << 20))
resource.setrlimit(resource.RLIMIT_FSIZE, (int(sys.argv[1]), int(sys.argv[1])))
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
os.execv(sys.argv[2],sys.argv[2:])
