"""Review raw recordings with an optional synchronized video; save separate labels."""
import argparse
import json
from pathlib import Path
import time
import numpy as np
from core import GESTURES, RAW_PERIOD_US, recording_metadata, load_csv, labels_for
from video_sync import VideoSync, VideoReader


class Annotator:
    def __init__(self,path,video=None):
        import matplotlib.pyplot as plt
        from matplotlib.widgets import SpanSelector, RadioButtons, Button, Slider, CheckButtons
        self.path=Path(path)
        recording_metadata(path)
        self.seq,self.times,self.x=load_csv(path)
        self.seconds=(self.times-self.times[0])/1e6
        if len(self.times)<2: raise ValueError('Annotation requires at least two samples')
        self.out=self.path.with_suffix('.labels.json')
        self.doc=json.loads(self.out.read_text()) if self.out.exists() else dict(reviewed=False,intervals=[])
        self.video_path=Path(video).resolve() if video else None
        self.reader=VideoReader(video) if video else None
        self.sync=VideoSync(); self.points=[]
        saved=self.doc.get('video_sync',{})
        if self.video_path and saved.get('video')==str(self.video_path):
            self.sync=VideoSync(saved['offset'],saved['rate']); self.points=saved.get('points',[])
        self.cursor=0.; self.video_seconds=0.; self.mark=None
        self.playing=False; self.busy=False; self.dirty=False; self.overlays=[]; self.widgets=[]
        self.fig=plt.figure(figsize=(16,9))
        self.axes=[self.fig.add_axes([.16,.60,.47,.29]),self.fig.add_axes([.16,.27,.47,.29])]
        self.axes[1].sharex(self.axes[0])
        # Decimate display only; annotations/export still use full-rate data.
        stride=max(1,len(self.times)//60000)
        for i in range(3):
            self.axes[0].plot(self.seconds[::stride],self.x[::stride,i],label='xyz'[i],lw=.6)
            self.axes[1].plot(self.seconds[::stride],self.x[::stride,i+3],label='xyz'[i],lw=.6)
        for ax in self.axes: ax.legend(loc='upper right'); ax.set_xlim(0,self.seconds[-1])
        self.axes[0].set_ylabel('Acceleration (g)'); self.axes[1].set_ylabel('Gyro (deg/s)')
        self.axes[1].set_xlabel('Sensor seconds from first reading')
        self.lines=[ax.axvline(0,color='black',lw=1) for ax in self.axes]
        self.video_ax=self.fig.add_axes([.67,.38,.31,.48]); self.video_ax.axis('off')
        self.image=None
        if not self.reader: self.video_ax.text(.1,.5,'Plot-only mode\nUse --video to load a recording.')
        self.radio=RadioButtons(self.fig.add_axes([.01,.43,.10,.43]),(*GESTURES,'ignore'))
        self.widgets.append(self.radio)
        self.link=CheckButtons(self.fig.add_axes([.67,.29,.17,.06]),['Link timelines'],[bool(self.points)])
        self.widgets.append(self.link)
        self.status=self.fig.text(.16,.015,'',fontsize=9)
        self.sensor_slider=Slider(self.fig.add_axes([.20,.20,.40,.022]),'Sensor s',0,float(self.seconds[-1]),valinit=0)
        self.sensor_slider.on_changed(self.seek_sensor); self.widgets.append(self.sensor_slider)
        self.video_slider=None
        if self.reader:
            self.video_slider=Slider(self.fig.add_axes([.72,.23,.23,.022]),'Video s',0,max(self.reader.duration,1/self.reader.fps),valinit=0)
            self.video_slider.on_changed(self.seek_video); self.widgets.append(self.video_slider)
            self.show_video(0)
        def button(rect,label,callback):
            b=Button(self.fig.add_axes(rect),label); b.on_clicked(callback); self.widgets.append(b)
        button([.16,.12,.09,.04],'Mark start',lambda _:self.mark_start())
        button([.26,.12,.09,.04],'Mark end',lambda _:self.mark_end())
        button([.36,.12,.10,.04],'Undo last',lambda _:self.undo())
        button([.47,.12,.16,.04],'Delete at cursor',lambda _:self.delete())
        button([.16,.065,.13,.04],'Save draft',lambda _:self.save(False))
        button([.30,.065,.23,.04],'Save reviewed labels',lambda _:self.save(True))
        button([.67,.17,.09,.04],'Play / pause',lambda _:self.toggle_play())
        button([.77,.17,.095,.04],'Frame <',lambda _:self.step_frame(-1))
        button([.875,.17,.095,.04],'Frame >',lambda _:self.step_frame(1))
        button([.67,.11,.145,.04],'Add sync point',lambda _:self.add_sync())
        button([.83,.11,.14,.04],'Reset sync',lambda _:self.reset_sync())
        self.spans=[SpanSelector(ax,self.select,'horizontal',useblit=True,props=dict(alpha=.2,facecolor='orange'),minspan=RAW_PERIOD_US/1e6) for ax in self.axes]
        self.fig.canvas.mpl_connect('button_press_event',self.click)
        self.fig.canvas.mpl_connect('close_event',self.close)
        self.timer=self.fig.canvas.new_timer(interval=40); self.timer.add_callback(self.tick)
        self.fig.suptitle('Choose symbol / ignore → drag its interval, or seek then Mark start / Mark end. Save after review.')
        self.redraw()

    def message(self,text): self.status.set_text(text); self.fig.canvas.draw_idle()
    def linked(self): return self.reader is not None and self.link.get_status()[0]
    def toolbar_active(self): return bool(self.fig.canvas.toolbar and self.fig.canvas.toolbar.mode)

    def show_video(self,value):
        if not self.reader: return
        if value<0 or value>self.reader.duration:
            if self.image is not None: self.image.set_visible(False)
            self.video_ax.set_title('Outside recorded video')
            return
        try: frame,actual=self.reader.frame(value)
        except ValueError as e: self.playing=False; self.message(str(e)); return
        self.video_seconds=actual
        if self.image is None: self.image=self.video_ax.imshow(frame)
        else: self.image.set_data(frame); self.image.set_visible(True)
        self.video_ax.set_title(f'Video {actual:.3f} s | offset {self.sync.offset:.3f}, rate {self.sync.rate:.6f}')
        if self.video_slider: self.video_slider.set_val(actual)

    def seek_sensor(self,value):
        if self.busy: return
        self.busy=True
        try:
            self.cursor=float(np.clip(value,0,self.seconds[-1]))
            self.sensor_slider.set_val(self.cursor)
            for line in self.lines: line.set_xdata([self.cursor,self.cursor])
            if self.linked(): self.show_video(self.sync.video_time(self.cursor))
        finally: self.busy=False
        self.fig.canvas.draw_idle()

    def seek_video(self,value):
        if self.busy or not self.reader: return
        self.busy=True
        try: self.show_video(float(value))
        finally: self.busy=False
        if self.linked(): self.seek_sensor(self.sync.sensor_time(self.video_seconds))
        self.fig.canvas.draw_idle()

    def click(self,event):
        if event.inaxes in self.axes and event.xdata is not None and not self.toolbar_active(): self.seek_sensor(event.xdata)

    def toggle_play(self):
        if not self.reader: self.message('Load a video using --video first.'); return
        self.playing=not self.playing
        self.play_start=time.monotonic(); self.play_video=self.video_seconds
        if self.playing: self.timer.start()
        else: self.timer.stop()

    def tick(self):
        if not self.playing: return
        value=self.play_video+(time.monotonic()-self.play_start)
        if value>=self.reader.duration: value=self.reader.duration; self.playing=False; self.timer.stop()
        self.seek_video(value)

    def step_frame(self,direction):
        self.playing=False; self.timer.stop()
        if self.reader: self.seek_video(np.clip(self.video_seconds+direction/self.reader.fps,0,self.reader.duration))

    def add_sync(self):
        if not self.reader: self.message('Load a video first.'); return
        if self.linked(): self.message('Uncheck Link timelines; independently locate the SAME event in plot and video.'); return
        if len(self.points)>=2: self.message('Two points already set. Reset sync to replace them.'); return
        points=self.points+[[self.cursor,self.video_seconds]]
        try: sync=VideoSync.from_points(points)
        except ValueError as e: self.message(str(e)); return
        self.points=points; self.sync=sync; self.dirty=True
        self.message(f'Saved alignment point {len(points)}. Link timelines to check; save labels to persist alignment.')

    def reset_sync(self):
        self.points=[]; self.sync=VideoSync(); self.dirty=True
        if self.linked(): self.link.set_active(0)
        self.message('Alignment reset. Match a visible event in both timelines, then Add sync point.')

    def mark_start(self):
        self.mark=self.cursor; self.message(f'Start marked at {self.mark:.3f} s. Seek end, then Mark end (exclusive).')

    def mark_end(self):
        if self.mark is None: self.message('Mark start first.'); return
        if self.cursor<=self.mark: self.message('End must be after start.'); return
        if self.select(self.mark,self.cursor): self.mark=None

    def select(self,a,b):
        if self.toolbar_active(): return False
        start=max(int(self.times[0]),int(self.times[0])+round(a*1e6))
        end=min(int(self.times[-1])+RAW_PERIOD_US,int(self.times[0])+round(b*1e6))
        if end-start<RAW_PERIOD_US: self.message('Select at least 2 ms. Zoom before labelling.'); return False
        if any(start<s['end_us'] and end>s['start_us'] for s in self.doc['intervals']):
            self.message('Intervals cannot overlap. Delete at cursor or Undo last first.'); return False
        if self.radio.value_selected!='ignore':
            gaps=np.flatnonzero((np.diff(self.seq)!=1)|(np.diff(self.times)!=RAW_PERIOD_US))
            if any(start<self.times[k+1] and end>self.times[k] for k in gaps):
                self.message('Symbol crosses a sensor gap. Mark the unusable interval ignore instead.'); return False
        self.doc['intervals'].append(dict(label=self.radio.value_selected,start_us=start,end_us=end))
        self.changed(); return True

    def changed(self): self.doc['reviewed']=False; self.dirty=True; self.redraw()
    def undo(self):
        if self.doc['intervals']: self.doc['intervals'].pop(); self.changed()
    def delete(self):
        t=int(self.times[0])+round(self.cursor*1e6)
        for i,a in enumerate(self.doc['intervals']):
            if a['start_us']<=t<a['end_us']: self.doc['intervals'].pop(i); self.changed(); return
        self.message('Click inside an annotated interval first.')

    def redraw(self):
        for obj in self.overlays: obj.remove()
        self.overlays=[]
        for a in self.doc['intervals']:
            lo,hi=(a['start_us']-int(self.times[0]))/1e6,(a['end_us']-int(self.times[0]))/1e6
            color='gray' if a['label']=='ignore' else f'C{GESTURES.index(a["label"])}'
            for ax in self.axes: self.overlays.append(ax.axvspan(lo,hi,alpha=.17,color=color))
            self.overlays.append(self.axes[0].text(lo,.97,a['label'],transform=self.axes[0].get_xaxis_transform(),va='top',fontsize=8))
        self.message(f'{len(self.doc["intervals"])} intervals. Unlabelled = background. Ignore = excluded. Save before closing.')

    def save(self,reviewed):
        self.doc['reviewed']=reviewed
        if self.reader:
            self.doc['video_sync']=dict(video=str(self.video_path),offset=self.sync.offset,rate=self.sync.rate,points=self.points)
        temp=self.out.with_suffix('.json.tmp')
        temp.write_text(json.dumps(self.doc,indent=2))
        # Validate before replacing existing annotations. Drafts validate using a reviewed copy.
        check=dict(self.doc,reviewed=True); temp.write_text(json.dumps(check,indent=2))
        try: labels_for(temp,self.times)
        except ValueError as e: self.message(f'Not saved: {e}'); return
        temp.write_text(json.dumps(self.doc,indent=2)); temp.replace(self.out)
        self.dirty=False; self.message(f'Saved {"reviewed" if reviewed else "draft"} labels to {self.out.name}.')

    def close(self,_=None):
        self.timer.stop()
        if self.reader: self.reader.close()
        if self.dirty:
            # Never silently mark partially reviewed work as reviewed.
            draft=self.out.with_suffix('.autosave.json')
            doc=dict(self.doc,reviewed=False)
            if self.reader: doc['video_sync']=dict(video=str(self.video_path),offset=self.sync.offset,rate=self.sync.rate,points=self.points)
            draft.write_text(json.dumps(doc,indent=2))
            print(f'Unsaved draft recovered to {draft}. Review before replacing the labels file.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('csv',type=Path); p.add_argument('--video',type=Path)
    args=p.parse_args()
    app=Annotator(args.csv,args.video)
    import matplotlib.pyplot as plt
    plt.show()


if __name__=='__main__': main()
