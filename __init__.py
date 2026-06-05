# -*- coding: utf-8 -*-
"""NDVI - Sen2Cor plugin loader."""

def classFactory(iface):
    from .ndvi_sen2cor import NdviSen2CorPlugin
    return NdviSen2CorPlugin(iface)
