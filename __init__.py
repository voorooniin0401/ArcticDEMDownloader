def classFactory(iface):
    from .arcticdem_downloader import ArcticDEMDownloaderPlugin
    return ArcticDEMDownloaderPlugin(iface)