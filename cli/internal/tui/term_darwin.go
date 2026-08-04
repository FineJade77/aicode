//go:build darwin

package tui

import "syscall"

// Darwin spells the termios ioctls TIOCGETA/TIOCSETA; Linux uses TCGETS/TCSETS.
// Split by build tag rather than resolved at runtime so a wrong constant is a
// compile error on the platform it is wrong for.
const (
	requestGetTermios = uintptr(syscall.TIOCGETA)
	requestSetTermios = uintptr(syscall.TIOCSETA)
)
