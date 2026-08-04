package runtimeio

import (
	"reflect"
	"testing"
)

func TestSelectedPathsMapsIndicesToFiles(t *testing.T) {
	paths := []string{"a.py", "b.py", "c.py"}

	if got := selectedPaths("1,3", paths); !reflect.DeepEqual(got, []string{"a.py", "c.py"}) {
		t.Fatalf("got = %#v", got)
	}
	if got := selectedPaths("2 3", paths); !reflect.DeepEqual(got, []string{"b.py", "c.py"}) {
		t.Fatalf("got = %#v", got)
	}
}

func TestAnOutOfRangeIndexSelectsNothing(t *testing.T) {
	// Dropping the bad index and applying the rest would silently write a
	// different set than the user asked for; retyping is the cheaper failure.
	paths := []string{"a.py", "b.py"}

	for _, answer := range []string{"3", "0", "1,9", "-1", "x", "1,x"} {
		if got := selectedPaths(answer, paths); got != nil {
			t.Fatalf("answer %q selected %#v", answer, got)
		}
	}
}

func TestStringListIgnoresNonStrings(t *testing.T) {
	got := stringList([]any{"a.py", 7, "b.py", nil})

	if !reflect.DeepEqual(got, []string{"a.py", "b.py"}) {
		t.Fatalf("got = %#v", got)
	}
	if stringList("not a list") != nil {
		t.Fatal("a non-list must not be read as a selection")
	}
}
