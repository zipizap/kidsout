#!/usr/bin/env bash

# go install -v golang.org/x/tools/cmd/godoc@latest

( sleep 3;  xdg-open "http://localhost:6060/pkg/$(basename $PWD)/?m=all" &>/dev/null & )
godoc -http=localhost:6060 


