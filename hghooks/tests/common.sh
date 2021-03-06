configurepushlog () {
  cat >> $1/.hg/hgrc << EOF
[extensions]
pushlog = $TESTDIR/hgext/pushlog
blackbox =

[blackbox]
track = pushlog

EOF

}

dumppushlog () {
  $TESTDIR/hghooks/tests/dumppushlog.py $TESTTMP/$1
}

configurehooks () {
  cat >> $1/.hg/hgrc << EOF
[extensions]
mozhooks = $TESTDIR/hghooks/mozhghooks/extension.py

[mozilla]
repo_root = $TESTTMP
EOF
}
