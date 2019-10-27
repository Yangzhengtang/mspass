#include <math.h>
#include "mspass/utility/MsPASSError.h"
#include "mspass/seismic/TimeWindow.h"
#include "mspass/seismic/CoreTimeSeries.h"
#include "mspass/seismic/Ensemble.h"
using namespace std;
namespace mspass {
using namespace mspass;
typedef Ensemble<CoreTimeSeries> TimeSeriesEnsemble;
/* This file is very very similar to a related set of procedures found
in the file three_component_helpers.cc.  Future efforts could merge
them into a template version standardizes what these do.
At the time of this conversion (Aug 2005) this and it's parent do
only one thing -- they contain procedures to take seismograms in
defined with an UTC time standard and convert them to an
"arrival time reference frame" .  See below for a description.

Author:  Gary L. Pavlis
	Indiana University
	pavlis@indiana.edu
*/


/* This function converts a seismogram from an absolute time standard to
an arrival time reference frame using an arrival time stored in the
metadata area of the object and referenced by a variable keyword.
A time window is relative time units defines the portion to be extracted.
If the window is larger than the data the whole array of data is copied
to the result.  If the window is smaller a subset is returned and
appropriate parameters are updated.

Arguments:
	tcsi - input data.  Assumed to be in the reference frame that
		matches the arrival time to be used to define time window
	arrival_time_key - metadata keyword string used to extract the
		time used to define time 0.  i.e. this is the keyword
		used to access a metadata field that defines the time
		that will become time 0 o the result.
	tw - TimeWindow in relative time units wrt arrival time defining
		section of data to be extracted.  Time 0 of the result
		will be the arrival time.

*/

shared_ptr<CoreTimeSeries> ArrivalTimeReference(CoreTimeSeries& tcsi,
        string arrival_time_key,
        TimeWindow tw)
{
    double atime;
    string base_error_message("ArrivalTimeReference: ");
    try
    {
        atime = tcsi.get_double(arrival_time_key);
        // Intentionally use the base class since the contents are discarded
        // get_double currently would throw a MetadataGetError
    } catch (MetadataGetError& mde)
    {
        throw MsPASSError(base_error_message
                          + arrival_time_key
                          + string(" not found in CoreTimeSeries object"),
                          ErrorSeverity::Invalid);
    }
    // We have to check this condition because ator will do nothing if
    // time is already relative and this condition cannot be tolerated
    // here as we have no idea what the time standard might be otherwise
    if(tcsi.tref == TimeReferenceType::Relative)
        throw MsPASSError(string("ArrivalTimeReference:  ")
                          + string("received data in relative time units\n")
                          + string("Cannot proceed as timing is ambiguous"),
                          ErrorSeverity::Invalid);

    // start with a clone of the original
    shared_ptr<CoreTimeSeries> tcso(new CoreTimeSeries(tcsi));
    tcso->ator(atime);  // shifts to arrival time relative time reference

    // Extracting a subset of the data is not needed when the requested
    // time window encloses all the data
    // Note an alternative approach is to pad with zeros and mark ends as
    // a gap, but here I view ends as better treated with variable
    // start and end times
    if( (tw.start > tcso->t0)  || (tw.end<tcso->time(tcso->ns-1) ) )
    {
        int jstart, jend;
        int ns_to_copy;
        int j,jj;
        jstart = tcso->sample_number(tw.start);
        jend = tcso->sample_number(tw.end);
        if(jstart<0) jstart=0;
        if(jend>=tcso->ns) jend = tcso->ns - 1;
        ns_to_copy = jend - jstart + 1;
        if(ns_to_copy<=0)
        {
            tcso->live=false;
            tcso->s.clear();
            return(tcso);
        }
        tcso->s.resize(ns_to_copy);
        tcso->ns=ns_to_copy;
        for(j=0,jj=jstart; j<ns_to_copy; ++j,++jj)
            tcso->s[j]=tcsi.s[jj];
        tcso->t0 += (tcso->dt)*static_cast<double>(jstart);
        //
        // This is necessary to allow for a rtoa (relative
        // to UTC time) conversion later.  Both of these
        // Modified somewhat for an older version that was deleted
        // by mistake in the previous version.
        // Before updating endtime was optional.  Now it is always
        // done.
        //
        if(jstart>0)
        {
            double stime=atime+tcso->t0;
            tcso->put("time",stime);
            // this one may not really be necessary
            tcso->put("endtime",atime+tcso->endtime());
        }
    }
    return(tcso);
}
/* Similar function to above but processes an entire ensemble.  The only
special thing it does is handle exceptions.  When the single object
processing function throws an exception the error is printed and the
object is simply not copied to the output ensemble.

This procedure is retained, but probably should be depricated to only
be used on error logging children of CoreTimeSeries that can post an
error like the one handled here to an error log.  Retained for now
as this functionality is essential. */
shared_ptr <TimeSeriesEnsemble> ArrivalTimeReference(TimeSeriesEnsemble& tcei,
        string arrival_time_key,
        TimeWindow tw)
{
    int nmembers=tcei.member.size();
    // use the special constructor to only clone the metadata and
    // set aside slots for the new ensemble.
    shared_ptr<TimeSeriesEnsemble>
    tceo(new TimeSeriesEnsemble(dynamic_cast<Metadata&>(tcei),
                                nmembers));
    tceo->member.reserve(nmembers);  // reserve this many slots for efficiency
    // We have to use a loop instead of for_each as I don't see how
    // else to handle errors cleanly here.
    vector<CoreTimeSeries>::iterator indata;
    shared_ptr<CoreTimeSeries> tcs;
    for(indata=tcei.member.begin(); indata!=tcei.member.end(); ++indata)
    {
        try {
            tcs=ArrivalTimeReference(*indata,
                                       arrival_time_key,tw);
            tceo->member.push_back(*tcs);
        } catch ( MsPASSError& serr)
        {
            tcs->live=false;
            tceo->member.push_back(*tcs);
            cerr << "Warning:  ArrivalTimeReference threw this error for current seismogram"<<endl;
            serr.log_error();
            cerr << "Seismogram was marked dead but copied to output Ensemble"
                <<endl;
        }
    }
    return(tceo);
}


} // end mspass namespace encapsulation
