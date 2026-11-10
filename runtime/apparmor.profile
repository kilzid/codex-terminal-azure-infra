# Based on Moby's Apache-2.0 Docker profile; namespace mount operations are enabled
# so an unprivileged user can create Codex's inner sandbox. No host capabilities are granted.
#include <tunables/global>
profile codex-terminal flags=(attach_disconnected,mediate_deleted) {
  #include <abstractions/base>
  network,
  deny network alg,
  deny network vsock,
  capability,
  file,
  mount,
  umount,
  pivot_root,
  signal,
  ptrace (trace,tracedby,read,readby) peer=codex-terminal,
  deny @{PROC}/* w,
  deny @{PROC}/{[^1-9/],[^1-9/][^0-9/],[^1-9s/][^0-9y/][^0-9s/],[^1-9/][^0-9/][^0-9/][^0-9/]*}/** w,
  deny @{PROC}/sys/[^k]** w,
  deny @{PROC}/sys/kernel/{?,??,[^s][^h][^m]**} w,
  deny @{PROC}/sysrq-trigger rwklx,
  deny @{PROC}/kcore rwklx,
  deny /sys/[^f]*/** wklx,
  deny /sys/f[^s]*/** wklx,
  deny /sys/fs/[^c]*/** wklx,
  deny /sys/fs/c[^g]*/** wklx,
  deny /sys/fs/cg[^r]*/** wklx,
  deny /sys/firmware/** rwklx,
  deny /sys/devices/virtual/powercap/** rwklx,
  deny /sys/kernel/security/** rwklx,
}
