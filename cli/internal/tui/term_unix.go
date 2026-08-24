//go:build darwin || linux

package tui

import (
	"fmt"
	"os"
	"syscall"
	"unsafe"
)

// Terminal owns raw mode and the alternate screen.
//
// Restoring is the part that matters: a process that exits without putting the
// terminal back leaves the user with no echo and no line editing, and the usual
// advice is to type `reset` blind. Every exit path here goes through Restore.
type Terminal struct {
	fd       int
	original syscall.Termios
	restored bool
}

type winsize struct{ rows, cols, x, y uint16 }

// OpenTerminal puts the terminal in raw mode and switches to the alternate
// screen. It fails rather than degrading when stdin is not a TTY: a full-screen
// view piped into a file is not a smaller version of itself, it is garbage.
func OpenTerminal() (*Terminal, error) {
	fd := int(os.Stdin.Fd())
	var original syscall.Termios
	if err := ioctlTermios(fd, requestGetTermios, &original); err != nil {
		return nil, fmt.Errorf("aicode tui needs a terminal (stdin is not a TTY): %w", err)
	}
	raw := original
	// Byte-at-a-time input with no echo and no signal generation: the view draws
	// every character itself, and Ctrl-C has to reach the event loop as a key so
	// it can cancel a run instead of killing the process mid-draw.
	raw.Lflag &^= syscall.ECHO | syscall.ICANON | syscall.ISIG | syscall.IEXTEN
	raw.Iflag &^= syscall.IXON | syscall.ICRNL | syscall.BRKINT | syscall.INPCK | syscall.ISTRIP
	raw.Oflag &^= syscall.OPOST
	raw.Cc[syscall.VMIN] = 1
	raw.Cc[syscall.VTIME] = 0
	if err := ioctlTermios(fd, requestSetTermios, &raw); err != nil {
		return nil, fmt.Errorf("could not enter raw mode: %w", err)
	}
	terminal := &Terminal{fd: fd, original: original}
	// Alternate screen, cursor hidden. Paired with the restore below.
	fmt.Fprint(os.Stdout, "\x1b[?1049h\x1b[?25l")
	return terminal, nil
}

// Restore is idempotent so a deferred call and an explicit one cannot fight.
func (t *Terminal) Restore() {
	if t == nil || t.restored {
		return
	}
	t.restored = true
	fmt.Fprint(os.Stdout, "\x1b[?25h\x1b[?1049l")
	_ = ioctlTermios(t.fd, requestSetTermios, &t.original)
}

// Size reports the window in rows and columns, falling back to a usable default
// when the ioctl fails rather than rendering into a zero-sized screen.
func (t *Terminal) Size() (int, int) {
	var ws winsize
	_, _, errno := syscall.Syscall(
		syscall.SYS_IOCTL,
		uintptr(t.fd),
		uintptr(syscall.TIOCGWINSZ),
		uintptr(unsafe.Pointer(&ws)),
	)
	if errno != 0 || ws.cols == 0 || ws.rows == 0 {
		return 80, 24
	}
	return int(ws.cols), int(ws.rows)
}

func ioctlTermios(fd int, request uintptr, termios *syscall.Termios) error {
	_, _, errno := syscall.Syscall6(
		syscall.SYS_IOCTL,
		uintptr(fd),
		request,
		uintptr(unsafe.Pointer(termios)),
		0, 0, 0,
	)
	if errno != 0 {
		return errno
	}
	return nil
}
