
export CFITSIOROOT=$PREFIX
export METISROOT=$PREFIX
export SUITESPARSEROOT=$PREFIX
cmake -DCMAKE_INSTALL_PREFIX=$PREFIX .
make
make install
