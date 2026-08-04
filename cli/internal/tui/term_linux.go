//go:build linux

package tui

import "syscall"

const (
	requestGetTermios = uintptr(syscall.TCGETS)
	requestSetTermios = uintptr(syscall.TCSETS)
)
