//go:build !darwin && !linux

package tui

import "errors"

// Terminal is a stub on platforms without the termios path implemented.
//
// Failing at the entry point is deliberate: the alternative is a build tag that
// silently produces a binary whose `tui` command corrupts the terminal.
type Terminal struct{}

func OpenTerminal() (*Terminal, error) {
	return nil, errors.New("aicode tui is available on macOS and Linux only")
}

func (t *Terminal) Restore()         {}
func (t *Terminal) Size() (int, int) { return 80, 24 }
